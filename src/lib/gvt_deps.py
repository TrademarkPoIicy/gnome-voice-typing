"""Missing-package check for the graphics-card engine and for ydotool.

The package names come from data/distro-packages.sh (the same lists install.sh uses).
Installing them is always an explicit Install click, and it always goes through pkexec
so the desktop shows its normal password window. This module never runs sudo.
"""
import glob, json, os, re, shlex, shutil, signal, subprocess, time

APP = 'gnome-voice-typing'
PKG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9+._-]*$')
PKEXEC_DISMISSED = 126   # polkit: the user closed the password window
PKEXEC_DENIED = 127      # polkit: not authorized, or pkexec could not run the command

# Model id on the processor -> the graphics-card model the switch should land on.
GPU_EQUIV = {
    'parakeet': 'parakeet-gpu',
    'small.en': 'small.en-gpu',
    'medium.en': 'medium.en-q5-gpu',
    'distil-large-v3': 'large-v3-turbo-gpu',
}
GPU_FILES = {
    'parakeet-gpu': 'ggml/ggml-parakeet-tdt-0.6b-v3-f16.bin',
    'small.en-gpu': 'ggml/ggml-small.en.bin',
    'medium.en-q5-gpu': 'ggml/ggml-medium.en-q5_0.bin',
    'large-v3-turbo-gpu': 'ggml/ggml-large-v3-turbo-q5_0.bin',
}
LIVE_PHASES = ('installing', 'building', 'downloading', 'switching')


class JobFailed(Exception):
    def __init__(self, message, cancelled=False):
        super().__init__(message)
        self.cancelled = cancelled


def install_root_from_here():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def package_script(root=None):
    return os.path.join(root or install_root_from_here(), 'data', 'distro-packages.sh')


def load_lists(root=None):
    """{'dnf': {'runtime': [...], 'build': [...], 'gpu': [...], 'query': str, 'ydotool_source': [...]}, ...}"""
    script = package_script(root)
    r = subprocess.run(['bash', script, '--json'], check=True, capture_output=True, text=True)
    data = json.loads(r.stdout)
    for pm, spec in data.items():
        for key in ('runtime', 'build', 'gpu', 'ydotool_source'):
            for name in spec[key]:
                if not PKG_RE.match(name):
                    raise ValueError('unsafe package name in %s %s: %r' % (pm, key, name))
    return data


def parse_os_release(text):
    out = {}
    for line in (text or '').splitlines():
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k] = v.strip().strip('"').strip("'")
    return out


def detect_pm(osr):
    blob = (osr.get('ID', '') + ' ' + osr.get('ID_LIKE', '')).lower()
    if any(s in blob for s in ('fedora', 'rhel', 'centos')):
        return 'dnf'
    if any(s in blob for s in ('debian', 'ubuntu')):
        return 'apt'
    if any(s in blob for s in ('arch', 'manjaro')):
        return 'pacman'
    return ''


def parse_vulkan_summary(text):
    """First real Vulkan device name, matching install.sh's vulkaninfo awk. '' if only software rendering."""
    real = False
    name = ''
    found = None

    def flush():
        nonlocal real, name, found
        if found is None and real and name and not re.search(r'llvmpipe|lavapipe|SwiftShader', name):
            found = name
        real = False
        name = ''

    for line in (text or '').splitlines():
        if re.match(r'^GPU[0-9]+:', line):
            flush()
            continue
        if 'deviceType' in line and re.search(r'(DISCRETE|INTEGRATED|VIRTUAL)_GPU', line):
            real = True
        if 'deviceName' in line:
            name = re.sub(r'^[^=]*= *', '', line).strip()
    flush()
    return found or ''


def config_dir():
    return (os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')) + '/' + APP


def prefs_path():
    return config_dir() + '/system-deps.json'


def load_prefs(path=None):
    try:
        with open(path or prefs_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_prefs(path=None, **kw):
    path = path or prefs_path()
    data = load_prefs(path)
    data.update(kw)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    return data


def job_path():
    rt = os.environ.get('XDG_RUNTIME_DIR') or ('/tmp/%s-%d' % (APP, os.getuid()))
    d = rt + '/' + APP + '-overlay'
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return d + '/deps-job.json'


def allowlist(spec):
    names = []
    for key in ('runtime', 'build', 'gpu', 'ydotool_source'):
        names.extend(spec.get(key) or [])
    return [n for n in names if PKG_RE.match(n)]


def package_command(pm, packages, spec):
    """The package-manager argv pkexec will run. Refuses anything outside the distro list. No sudo."""
    allowed = set(allowlist(spec))
    clean = []
    for name in packages:
        if name in clean:
            continue
        if name not in allowed or not PKG_RE.match(name):
            raise ValueError('refusing package outside the distro list: %s' % name)
        clean.append(name)
    if not clean:
        raise ValueError('no packages')
    if pm == 'dnf':
        cmd = ['dnf', 'install', '-y', *clean]
    elif pm == 'pacman':
        cmd = ['pacman', '-S', '--needed', '--noconfirm', *clean]
    elif pm == 'apt':
        quoted = ' '.join(shlex.quote(p) for p in clean)
        cmd = ['bash', '-c', 'apt-get update && apt-get install -y ' + quoted]
    else:
        raise ValueError('no supported package manager')
    if 'sudo' in cmd:
        raise ValueError('refusing to run sudo')
    return ['pkexec', *cmd], clean


def manual_command(pm, packages):
    """A command the user can type themselves. Display only: never executed."""
    pkgs = ' '.join(packages)
    if pm == 'dnf':
        return 'sudo dnf install -y ' + pkgs
    if pm == 'pacman':
        return 'sudo pacman -S --needed --noconfirm ' + pkgs
    if pm == 'apt':
        return 'sudo apt-get update && sudo apt-get install -y ' + pkgs
    return ''


def classify_pkexec(code, stderr=''):
    if code == 0:
        return 'ok', ''
    if code == PKEXEC_DISMISSED:
        return 'cancelled', 'The password window was closed, so nothing was installed. Voice typing stays on the processor.'
    if code == PKEXEC_DENIED:
        return 'cancelled', 'Permission was not granted, so nothing was installed. Voice typing stays on the processor.'
    tail = ''
    for line in reversed((stderr or '').splitlines()):
        line = line.strip()
        if line:
            tail = line[:220]
            break
    msg = 'Package installation failed (exit %d). Voice typing stays on the processor.' % code
    if tail:
        msg += ' ' + tail
    return 'failed', msg


def _run_text(res):
    out = getattr(res, 'stdout', '') or ''
    err = getattr(res, 'stderr', '') or ''
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    if isinstance(err, bytes):
        err = err.decode('utf-8', 'replace')
    return out, err


def _query_ok(probe, query, pkg):
    if not query or query.strip() == 'true':
        return True
    try:
        res = probe.run(query.split() + [pkg], timeout=20)
    except Exception:
        return False
    return getattr(res, 'returncode', 1) == 0


def _missing(probe, query, names, skip=()):
    skip = set(skip)
    out = []
    for name in names:
        if not name or name in skip or name in out:
            continue
        if not _query_ok(probe, query, name):
            out.append(name)
    return out


def _ydotool_ok(probe):
    cands = []
    ext = probe.install_root() + '/ext/bin/ydotoold'
    if probe.exists(ext):
        cands.append(ext)
    found = probe.which('ydotoold')
    if found and found not in cands:
        cands.append(found)
    for exe in cands:
        try:
            res = probe.run([exe, '--help'], timeout=5)
        except Exception:
            continue
        out, err = _run_text(res)
        if '--socket-path' in (out + err):
            return True
    return False


def _apt_ydotool_old(probe):
    if not probe.which('apt-cache'):
        return False
    try:
        res = probe.run(['apt-cache', 'policy', 'ydotool'], timeout=15)
    except Exception:
        return False
    out, _ = _run_text(res)
    ver = ''
    for line in out.splitlines():
        if 'Candidate:' in line:
            parts = line.split()
            ver = parts[-1] if parts else ''
            break
    return ver in ('', '(none)') or ver.startswith('0.')


def _gpu_state(probe):
    exe = probe.which('vulkaninfo')
    nodes = list(probe.render_nodes() or [])
    if not exe:
        return {'state': 'unknown' if nodes else 'absent', 'name': ''}
    try:
        res = probe.run([exe, '--summary'], timeout=8)
    except Exception:
        return {'state': 'unknown' if nodes else 'absent', 'name': ''}
    out, _ = _run_text(res)
    name = parse_vulkan_summary(out)
    if name:
        return {'state': 'present', 'name': name}
    if getattr(res, 'returncode', 1) != 0 and not out.strip():
        return {'state': 'unknown' if nodes else 'absent', 'name': ''}
    return {'state': 'absent', 'name': ''}


def assess(probe, prefs=None, lists=None):
    """Snapshot of what is missing and what Install is allowed to do."""
    osr = parse_os_release(probe.os_release_text())
    pm = detect_pm(osr)
    lists = lists if lists is not None else load_lists()
    spec = lists.get(pm) or {'runtime': [], 'build': [], 'gpu': [], 'query': 'true', 'ydotool_source': []}
    query = spec.get('query') or 'true'
    gpu = _gpu_state(probe)
    offer_gpu = gpu['state'] != 'absent'
    gpu_built = probe.exists(probe.install_root() + '/gpu/libasrshim.so')
    typing_ok = _ydotool_ok(probe)
    build_ydotool = bool(pm == 'apt' and not typing_ok and _apt_ydotool_old(probe))

    missing_gpu = []
    if offer_gpu and not gpu_built and pm:
        missing_gpu = _missing(probe, query, list(spec.get('build') or []) + list(spec.get('gpu') or []))

    missing_typing = []
    if not typing_ok:
        if build_ydotool:
            missing_typing = _missing(probe, query, spec.get('ydotool_source') or [], skip=missing_gpu)
        elif pm and 'ydotool' in (spec.get('runtime') or []):
            missing_typing = _missing(probe, query, ['ydotool'])
        elif not pm:
            missing_typing = ['ydotool']

    install_packages = []
    for name in missing_typing + missing_gpu:
        if name not in install_packages:
            install_packages.append(name)

    pkexec = bool(probe.which('pkexec'))
    pkexec_argv = None
    manual = ''
    if install_packages and pm:
        argv, install_packages = package_command(pm, install_packages, spec)
        manual = manual_command(pm, install_packages)
        if pkexec:
            pkexec_argv = argv
        else:
            pkexec_argv = None

    want_build = bool(offer_gpu and not gpu_built)
    needs = bool(missing_gpu or missing_typing or build_ydotool or want_build)
    if prefs is None:
        prefs = probe.prefs()
    dismissed = bool(prefs.get('dismissed'))
    prompted = bool(prefs.get('prompted'))
    return {
        'pm': pm,
        'distro': osr.get('PRETTY_NAME') or osr.get('ID') or '',
        'pkexec': pkexec,
        'gpu': gpu,
        'offer_gpu': offer_gpu,
        'gpu_built': gpu_built,
        'typing_ok': typing_ok,
        'build_ydotool': build_ydotool,
        'missing_gpu': missing_gpu,
        'missing_typing': missing_typing,
        'install_packages': install_packages,
        'pkexec_argv': pkexec_argv,
        'manual_command': manual,
        'uinput_writable': bool(probe.access_write('/dev/uinput')),
        'needs_attention': needs,
        'want_gpu_build': want_build,
        'dismissed': dismissed,
        'prompted': prompted,
        'should_auto_prompt': bool(needs and not dismissed and not prompted),
    }


def present(st):
    """User-facing copy for the prompt. The extension renders this; it does not invent the list."""
    paragraphs = []
    groups = []
    gpu = st.get('gpu') or {}
    if st.get('offer_gpu') and not st.get('gpu_built'):
        if gpu.get('state') == 'present' and gpu.get('name'):
            paragraphs.append('A Vulkan graphics card was found (%s).' % gpu['name'])
        elif gpu.get('state') == 'unknown':
            paragraphs.append('A graphics device was found, but Vulkan itself could not be checked yet. If there is no usable Vulkan card, the graphics-card build is skipped after the packages are installed.')
        if st.get('missing_gpu'):
            paragraphs.append('Voice typing works on the processor without the packages below. The graphics-card version cannot be built until they are installed.')
            groups.append({'id': 'gpu', 'title': 'Missing packages for the graphics-card build', 'packages': list(st['missing_gpu'])})
        else:
            paragraphs.append('The build packages are already installed. The graphics-card engine still has to be built on this computer. Voice typing keeps using the processor until that finishes.')
    if not st.get('typing_ok'):
        if st.get('build_ydotool'):
            paragraphs.append("This distribution's ydotool package is too old to type with. Install builds ydotool 1.0.4 from source, which does not need a password.")
            if st.get('missing_typing'):
                groups.append({'id': 'typing', 'title': 'Missing packages to build ydotool', 'packages': list(st['missing_typing'])})
        else:
            paragraphs.append("ydotool is missing, so words are typed through GNOME's remote-control portal: a permission prompt, and an indicator on screen while you dictate.")
            if st.get('missing_typing'):
                groups.append({'id': 'typing', 'title': 'Missing package for typing', 'packages': list(st['missing_typing'])})
    if not st.get('pm') and st.get('needs_attention'):
        paragraphs.append('This distribution is not Fedora, Debian, Ubuntu or Arch, so the packages cannot be installed from here.')
    if st.get('install_packages') and not st.get('pkexec'):
        paragraphs.append('There is no graphical password prompt (pkexec) on this computer. Nothing will be installed automatically, and sudo will not be run. Install the packages yourself with the command below, then press Check again.')

    if st.get('offer_gpu') and not st.get('gpu_built') and st.get('missing_gpu'):
        title = 'Set up the graphics-card version?'
    elif st.get('offer_gpu') and not st.get('gpu_built'):
        title = 'Build the graphics-card engine?'
    elif st.get('build_ydotool'):
        title = 'Build ydotool for typing?'
    else:
        title = 'Install ydotool for typing?'

    if st.get('install_packages') and st.get('pkexec'):
        label = 'Install'
    elif st.get('install_packages'):
        label = 'Check again'
    else:
        label = 'Build'

    bits = []
    if st.get('missing_gpu'):
        n = len(st['missing_gpu'])
        bits.append('%d package%s missing for the graphics-card build' % (n, '' if n == 1 else 's'))
    elif st.get('offer_gpu') and not st.get('gpu_built'):
        bits.append('Graphics-card engine is not built')
    if not st.get('typing_ok'):
        bits.append('ydotool needs to be built' if st.get('build_ydotool') else 'ydotool is missing')
    return {
        'title': title,
        'paragraphs': paragraphs,
        'groups': groups,
        'footnote': 'Not now leaves voice typing as it is. You can open this again from Settings, under System packages. It will not be shown on every launch.',
        'install_label': label,
        'not_now_label': 'Not now',
        'manual_command': st.get('manual_command') or '',
        'settings_line': ('. '.join(bits) + '.') if bits else '',
        'settings_button': 'Review',
    }


def _last_line(path):
    try:
        with open(path, errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return ''
    for line in reversed(lines):
        line = line.strip()
        if line:
            return line[:240]
    return ''


def success_message(st, built_gpu):
    parts = []
    if built_gpu and st.get('gpu_built'):
        parts.append('The graphics-card engine is ready, and voice typing is switching to it.')
    elif built_gpu and (st.get('gpu') or {}).get('state') == 'absent':
        parts.append('No Vulkan graphics card was found, so the graphics-card engine was not built. Voice typing stays on the processor.')
    if st.get('typing_ok'):
        if st.get('uinput_writable'):
            parts.append('ydotool can type into the focused window.')
        else:
            parts.append("ydotool is ready. Typing still needs write access to /dev/uinput, so until that is allowed GNOME's remote-control prompt is used. install.sh can add that rule, and it asks before it does.")
    elif not parts:
        parts.append('Finished.')
    return ' '.join(parts)


class SystemProbe:
    """The real machine. Tests use a stand-in with the same methods."""

    def __init__(self, root=None):
        self._root = root or install_root_from_here()

    def install_root(self):
        return self._root

    def os_release_text(self):
        try:
            with open('/etc/os-release') as f:
                return f.read()
        except OSError:
            return ''

    def which(self, name):
        return shutil.which(name)

    def run(self, argv, timeout=20):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def render_nodes(self):
        return sorted(glob.glob('/dev/dri/renderD*'))

    def exists(self, path):
        return os.path.exists(path)

    def access_write(self, path):
        return os.access(path, os.W_OK)

    def prefs(self):
        return load_prefs()


class SystemRunner:
    def __init__(self, root=None):
        self.root = root or install_root_from_here()
        self._children = []

    def children(self):
        return list(self._children)

    def pkexec(self, argv):
        if not argv or argv[0] != 'pkexec' or 'sudo' in argv:
            raise JobFailed('Refusing to install packages without pkexec.')
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def _popen(self, argv):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self._children.append(proc)
        return proc

    def _wait_log(self, proc, log_path, reporter, phase, message):
        while proc.poll() is None:
            reporter({'phase': phase, 'message': message, 'detail': _last_line(log_path), 'ok': False, 'cancelled': False})
            time.sleep(0.4)
        return proc.returncode

    def build(self, reporter):
        script = self.root + '/scripts/build-gpu.sh'
        log = _cache_dir() + '/build.log'
        proc = self._popen(['bash', script, self.root])
        return self._wait_log(proc, log, reporter, 'building',
                              'Building the graphics-card engine… this usually takes several minutes.')

    def build_ydotool(self, reporter):
        script = self.root + '/scripts/build-ydotool.sh'
        log = _cache_dir() + '/ydotool/build.log'
        proc = self._popen(['bash', script, self.root])
        return self._wait_log(proc, log, reporter, 'building', 'Building ydotool 1.0.4 from source…')

    def choose_model(self):
        mid = 'parakeet'
        try:
            mid = json.load(open(config_dir() + '/config.json')).get('model') or mid
        except Exception:
            pass
        if mid in GPU_FILES:
            return mid
        return GPU_EQUIV.get(mid, 'parakeet-gpu')

    def model_installed(self, mid):
        rel = GPU_FILES.get(mid)
        return bool(rel) and os.path.isfile(self.root + '/models/' + rel)

    def install_model(self, mid, reporter):
        tool = self.root + '/bin/voice-typing-models'
        status = self.root + '/models/.status/' + mid + '.json'
        proc = self._popen([tool, 'install', mid])
        while proc.poll() is None:
            detail = ''
            try:
                s = json.load(open(status))
                detail = s.get('msg') or ('%d%%' % int(s.get('pct') or 0))
            except Exception:
                detail = ''
            reporter({'phase': 'downloading', 'message': 'Downloading the graphics-card speech model…',
                      'detail': detail, 'ok': False, 'cancelled': False})
            time.sleep(0.4)
        return proc.returncode

    def apply_model(self, mid):
        cli = self.root + '/bin/voice-typing'
        res = subprocess.run([cli, '--apply', json.dumps({'model': mid, 'device': 'gpu'})],
                             capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        return res.returncode

    def log_tail(self):
        return _last_line(_cache_dir() + '/build.log') or _last_line(_cache_dir() + '/cmake.log')


def _cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return base + '/' + APP + '/build'


def run_job(probe, runner, reporter):
    """Install missing packages with pkexec, then build the graphics-card engine and switch to it.

    `reporter` is called with a job dict (phase, message, detail, ok, cancelled) on each step.
    A cancelled password window stops the job before any build.
    """
    first = assess(probe)
    want_gpu = bool(first.get('want_gpu_build'))
    build_yd = bool(first.get('build_ydotool'))
    if not first.get('needs_attention'):
        reporter({'phase': 'done', 'ok': True, 'cancelled': False, 'message': 'Nothing to install. Voice typing is already set up.', 'detail': ''})
        return first
    try:
        if first.get('install_packages'):
            if not first.get('pm'):
                raise JobFailed('This distribution is not Fedora, Debian, Ubuntu or Arch, so the packages cannot be installed from here.')
            if not first.get('pkexec') or not first.get('pkexec_argv'):
                cmd = first.get('manual_command') or ''
                raise JobFailed('There is no graphical password prompt (pkexec), so nothing was installed. '
                                'Install the packages yourself, then press Check again.' + (('\n' + cmd) if cmd else ''))
            argv = first['pkexec_argv']
            if argv[0] != 'pkexec' or 'sudo' in argv:
                raise JobFailed('Refusing to install packages without pkexec.')
            reporter({'phase': 'installing', 'message': 'Installing packages… a password window should open. Close that window to cancel.',
                      'detail': '', 'ok': False, 'cancelled': False})
            res = runner.pkexec(argv)
            kind, msg = classify_pkexec(getattr(res, 'returncode', 1), getattr(res, 'stderr', '') or '')
            if kind != 'ok':
                raise JobFailed(msg, cancelled=(kind == 'cancelled'))
            after = assess(probe, prefs={})
            if after.get('install_packages'):
                raise JobFailed('These packages are still missing: %s' % ', '.join(after['install_packages']))
        if build_yd and not assess(probe, prefs={}).get('typing_ok'):
            reporter({'phase': 'building', 'message': 'Building ydotool 1.0.4 from source…', 'detail': '', 'ok': False, 'cancelled': False})
            if runner.build_ydotool(reporter) != 0:
                raise JobFailed('Building ydotool from source failed. Voice typing keeps using the remote-control portal.')
        if want_gpu:
            now = assess(probe, prefs={})
            if now['gpu']['state'] == 'absent':
                done = assess(probe, prefs={})
                reporter({'phase': 'done', 'ok': True, 'cancelled': False, 'message': success_message(done, True), 'detail': ''})
                return done
            if now['gpu']['state'] != 'present':
                raise JobFailed('Vulkan still could not be checked, so the graphics-card engine was not built. Voice typing stays on the processor.')
            if now.get('missing_gpu'):
                raise JobFailed('The graphics-card build tools are still missing: %s' % ', '.join(now['missing_gpu']))
            reporter({'phase': 'building', 'message': 'Building the graphics-card engine… this usually takes several minutes.',
                      'detail': '', 'ok': False, 'cancelled': False})
            if runner.build(reporter) != 0:
                tail = ''
                try:
                    tail = runner.log_tail()
                except Exception:
                    tail = ''
                raise JobFailed('The graphics-card build failed. Voice typing stays on the processor.' + (('\n' + tail) if tail else ''))
            mid = runner.choose_model()
            if not runner.model_installed(mid):
                reporter({'phase': 'downloading', 'message': 'Downloading the graphics-card speech model…',
                          'detail': mid, 'ok': False, 'cancelled': False})
                if runner.install_model(mid, reporter) != 0:
                    raise JobFailed('The graphics-card engine is built, but the speech model did not download. Open Settings and press Download next to a graphics-card model.')
            reporter({'phase': 'switching', 'message': 'Switching voice typing to the graphics card…',
                      'detail': mid, 'ok': False, 'cancelled': False})
            if runner.apply_model(mid) != 0:
                raise JobFailed('The graphics-card engine is built, but switching to it failed. Choose a graphics-card model in Settings and press Save.')
        done = assess(probe, prefs={})
        reporter({'phase': 'done', 'ok': True, 'cancelled': False, 'message': success_message(done, want_gpu), 'detail': ''})
        return done
    except JobFailed as e:
        reporter({'phase': 'error', 'ok': False, 'cancelled': bool(e.cancelled), 'message': str(e), 'detail': ''})
        return None
    except Exception as e:
        reporter({'phase': 'error', 'ok': False, 'cancelled': False,
                  'message': 'Something went wrong: %s' % e, 'detail': ''})
        return None


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def cli_install():
    """Entry used by the Install button. Writes progress to the overlay job file."""
    path = job_path()
    runner = SystemRunner()

    def reporter(doc):
        doc = dict(doc)
        doc['pid'] = os.getpid()
        doc.setdefault('cancelled', False)
        doc.setdefault('ok', False)
        doc.setdefault('detail', '')
        _write_json(path, doc)

    def on_term(sig, frm):
        for proc in runner.children():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        reporter({'phase': 'error', 'ok': False, 'cancelled': True,
                  'message': 'Stopped. Voice typing stays on the processor.', 'detail': ''})
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, on_term)
    reporter({'phase': 'installing', 'message': 'Starting…', 'detail': '', 'ok': False, 'cancelled': False})
    run_job(SystemProbe(), runner, reporter)
    return 0


def cli_cancel():
    try:
        job = json.load(open(job_path()))
    except Exception:
        return 0
    pid = int(job.get('pid') or 0)
    if pid <= 1 or pid == os.getpid():
        return 0
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return 0
