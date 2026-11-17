#!/usr/bin/env python3
"""Model manager for gnome-voice-typing. Downloads ONLY from the pinned URLs in data/models-registry.json and
verifies every file's SHA-256 before it is used. Nothing else in the program touches the network.
The Settings panel runs this same tool (one-click Download / Uninstall); progress is written to models/.status/<id>.json.

  voice-typing-models list
  voice-typing-models status [--json]            every model with its state (installed, missing, downloading, failed, ...)
  voice-typing-models install ID [ID...] [--dry-run] [--force]
  voice-typing-models cancel ID                  stop a running download (it can be resumed later)
  voice-typing-models remove ID [ID...] [--active ID] [--force]    uninstall; refuses the model that is in use
  voice-typing-models verify [ID...]             re-check installed models against their SHA-256
  voice-typing-models adopt [ID...]              verify models downloaded by an older version and mark them installed
  voice-typing-models licenses
  voice-typing-models deps [--json]              missing system packages for the graphics-card engine / typing (first-launch check)
  voice-typing-models deps install COMPONENT     install only the selected package or setup component
  voice-typing-models deps ack                   acknowledge the completed setup message
  voice-typing-models deps dismiss|undismiss     "Not now" in the first-launch prompt / show it again

A model counts as installed only when EVERY file listed in the registry exists with the right size AND a marker
(models/.installed/<id>.json, written after the SHA-256 check) matches the registry. Downloads are staged in models/.dl/<id>/
(*.part, resumable) and moved into place only when complete, so a half-downloaded model never sits in the models folder.
"""
import shlex
import fcntl
import package_plan
import hashlib, importlib.util, json, os, shutil, signal, socket, subprocess, sys, tarfile, time, urllib.request, urllib.error

N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = json.load(open(N + '/data/models-registry.json'))['models']
CAT = {e['id']: e for e in json.load(open(N + '/data/models.json'))}
CATORDER = [e['id'] for e in json.load(open(N + '/data/models.json'))]
MODELS = N + '/models'
STATUS = MODELS + '/.status'
MARK = MODELS + '/.installed'
STAGE = MODELS + '/.dl'
UA = 'gnome-voice-typing/0.1 (model installer)'
ACTIVE = ('downloading', 'verifying', 'installing', 'building')

# Some networks have a broken IPv6 route to the model hosts (connections hang). Try IPv4 addresses first; IPv6 is still used if it is all there is.
_gai = socket.getaddrinfo
def _gai_v4_first(*a, **k): return sorted(_gai(*a, **k), key=lambda x: x[0] != socket.AF_INET)
socket.getaddrinfo = _gai_v4_first

class Cancelled(Exception): pass
class Fail(Exception):
    """A problem with a short, human-readable message (shown in the Settings panel)."""
_critical = False; _cancel_pending = False
def _on_term(sig, frm):
    global _cancel_pending
    if _critical: _cancel_pending = True; return
    raise Cancelled()

def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB': return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024.0

# ---------------------------------------------------------------- what a model consists of
def archive_of(mid): return REG[mid].get('archive')
def expected(mid):
    """[(relative path under models/, size, sha256 or None)] - every file that must exist for the model to count as installed."""
    r = REG[mid]
    if 'archive' in r:
        a = r['archive']; base = '%s/%s' % (a['dest'], a['dir'])
        if a.get('expect'): return [('%s/%s' % (base, n), s, None) for n, s in sorted(a['expect'].items())]
        return [(base + '/tokens.txt', None, None)]
    return [(f['dest'], f['size'], f['sha256']) for f in r['files']]
def total_size(mid):
    r = REG[mid]
    return r['archive']['size'] if 'archive' in r else sum(f['size'] for f in r['files'])
def fingerprint(mid): return hashlib.sha256(json.dumps(expected(mid), sort_keys=True).encode()).hexdigest()
def files_ok(mid):
    for rel, size, _ in expected(mid):
        p = MODELS + '/' + rel
        if not os.path.isfile(p) or (size is not None and os.path.getsize(p) != size): return False
    return True
def read_marker(mid):
    try:
        m = json.load(open('%s/%s.json' % (MARK, mid)))
        return m if m.get('fingerprint') == fingerprint(mid) else None
    except Exception: return None
def setup_needed(mid):
    """None, or what is still missing for an otherwise complete model: 'engine' (Vulkan backend not built) or 'package' (faster-whisper)."""
    r = REG[mid]
    if r.get('needs') == 'vulkan' and not os.path.exists(N + '/gpu/libasrshim.so'): return 'engine'
    if r.get('needs') == 'faster-whisper' and importlib.util.find_spec('faster_whisper') is None: return 'package'
    return None
def model_state(mid):
    """'missing' | 'unverified' (all files present, not yet checked) | 'setup' | 'installed'"""
    if not files_ok(mid): return 'missing'
    if not read_marker(mid): return 'unverified'
    return 'setup' if setup_needed(mid) else 'installed'
def is_installed(mid): return model_state(mid) == 'installed'

def sha256(path, job=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 22), b''):
            h.update(b)
            if job: job.hashed(len(b))
    return h.hexdigest()

# ---------------------------------------------------------------- status files (read by the Settings panel)
def _sfile(mid): return '%s/%s.json' % (STATUS, mid)
def _pid_is_ours(pid):
    try:
        os.kill(pid, 0)
        return 'models_tool' in open('/proc/%d/cmdline' % pid).read()
    except Exception: return False
def write_status(mid, **kw):
    os.makedirs(STATUS, exist_ok=True)
    d = dict(kw, id=mid, pid=os.getpid(), t=round(time.time(), 1)); tmp = '%s.tmp.%d' % (_sfile(mid), os.getpid())
    with open(tmp, 'w') as f: json.dump(d, f)
    os.replace(tmp, _sfile(mid))
def clear_status(mid):
    try: os.unlink(_sfile(mid))
    except OSError: pass
def read_status(mid):
    try: s = json.load(open(_sfile(mid)))
    except Exception: return None
    if s.get('status') in ACTIVE and not _pid_is_ours(s.get('pid', 0)):
        s = {'id': mid, 'status': 'failed', 'msg': 'The download was interrupted. Press Retry to continue where it stopped.', 'pid': 0, 't': time.time()}
        try: write_status(mid, status='failed', msg=s['msg'])
        except OSError: pass
    return s

class Job:
    """Progress reporting for one install: status file (throttled) + console output."""
    def __init__(self, mid):
        self.mid = mid; self.total = max(1, total_size(mid)); self.base = 0; self.got = 0; self.t0 = time.time(); self.lastw = 0; self.lastp = 0
        self.status = 'downloading'; self.msg = ''; self.hsh = 0; self.hsh_total = 1; self.tty = sys.stdout.isatty()
    def set(self, status, msg=''):
        self.status = status; self.msg = msg; self._write(force=True)
        if msg: print('  ' + msg, flush=True)
    def _write(self, force=False):
        now = time.time()
        if not force and now - self.lastw < 0.25: return
        self.lastw = now
        if self.status == 'verifying': pct = int(100 * self.hsh / max(1, self.hsh_total)); got = self.hsh; tot = self.hsh_total
        else: got = self.base + self.got; tot = self.total; pct = int(100 * got / tot)
        write_status(self.mid, status=self.status, pct=max(0, min(100, pct)), got=got, total=tot, msg=self.msg)
    def progress(self, got_in_file, label):
        self.got = got_in_file; self._write()
        now = time.time()
        if now - self.lastp > (0.5 if self.tty else 10):
            self.lastp = now; done = self.base + got_in_file
            msg = '  %s  %s / %s' % (label, human(done), human(self.total))
            print(('\r' + msg + '   ') if self.tty else msg, end='' if self.tty else '\n', flush=True)
    def file_done(self, size): self.base += size; self.got = 0
    def begin_hash(self, total): self.hsh = 0; self.hsh_total = max(1, total)
    def hashed(self, n): self.hsh += n; self._write()

# ---------------------------------------------------------------- downloading
def friendly(e):
    if isinstance(e, Fail): return str(e)
    if isinstance(e, urllib.error.HTTPError): return 'The download server answered "%s %s". Try again later.' % (e.code, e.reason)
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)): return "Couldn't reach the download server. Check your internet connection and press Retry."
    if isinstance(e, OSError) and e.errno == 28: return 'Not enough free disk space.'
    if isinstance(e, PermissionError): return "Couldn't write to the models folder (permission denied)."
    s = ' '.join(str(e).split())
    return ('Something went wrong: ' + s[:110]) if s else 'Something went wrong. Press Retry.'

def download(url, dest, sha, size, label, job):
    """Resumable download to dest.part, SHA-256 verified, then renamed to dest. Raises Fail on a checksum mismatch, Cancelled on cancel."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        job.set('verifying'); job.begin_hash(size)
        if sha256(dest, job) == sha: job.set('downloading'); job.file_done(size); print('  already present and verified: %s' % os.path.basename(dest)); return
        os.unlink(dest)
    job.set('downloading')
    part = dest + '.part'; last = None
    for attempt in range(1, 6):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size: os.unlink(part); have = 0
        try:
            if have < size:
                req = urllib.request.Request(url, headers={'User-Agent': UA, **({'Range': 'bytes=%d-' % have} if have else {})})
                with urllib.request.urlopen(req, timeout=30) as r:
                    if have and r.status != 206: have = 0     # server ignored Range: start over
                    got = have
                    with open(part, 'ab' if have else 'wb') as f:
                        while True:
                            b = r.read(1 << 20)
                            if not b: break
                            f.write(b); got += len(b); job.progress(got, label)
                    if job.tty: print()
            if os.path.getsize(part) != size: raise IOError('size %d != expected %d' % (os.path.getsize(part), size))
            job.set('verifying'); job.begin_hash(size)
            if sha256(part, job) != sha:
                os.unlink(part)
                raise Fail("The download was corrupted (its checksum didn't match), so it was discarded. Press Retry to download it again.")
            os.replace(part, dest); job.set('downloading'); job.file_done(size); return
        except (Fail, Cancelled): raise
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 416 and os.path.exists(part): os.unlink(part)       # stale partial file: start over
            elif 400 <= e.code < 500 and e.code != 429: raise
        except (urllib.error.URLError, IOError, OSError, TimeoutError, socket.timeout) as e:
            last = e
            if isinstance(e, OSError) and e.errno == 28: raise
        print('  attempt %d failed: %s' % (attempt, last), file=sys.stderr, flush=True)
        job.status = 'downloading'; job.msg = ''
        time.sleep(1.5 * attempt)
    raise last or Fail('The download failed.')

def safe_extract(tf, dest, top):
    dest = os.path.realpath(dest)
    for m in tf.getmembers():
        p = os.path.realpath(os.path.join(dest, m.name))
        if not (p == dest or p.startswith(dest + os.sep)) or not (m.isfile() or m.isdir()): continue   # no links, devices, or path tricks
        if not (m.name == top or m.name.startswith(top + '/')): continue
        if '/test_wavs' in m.name: continue          # not needed
        try: tf.extract(m, dest, filter='data')
        except TypeError: tf.extract(m, dest)      # Python < 3.12

def engine_tools_missing():
    return [t for t in ('git', 'cmake', 'c++', 'glslc') if not shutil.which(t)]
def write_marker(mid):
    os.makedirs(MARK, exist_ok=True)
    d = {'id': mid, 'fingerprint': fingerprint(mid), 'files': {rel: size for rel, size, _ in expected(mid)}, 'verified': time.strftime('%F %T'), 'by': 'models_tool 0.1.1'}
    tmp = '%s/%s.json.tmp.%d' % (MARK, mid, os.getpid())
    with open(tmp, 'w') as f: json.dump(d, f)
    os.replace(tmp, '%s/%s.json' % (MARK, mid))

def verify_files(mid, job=None, delete_bad=False):
    """Full SHA-256 check of an installed model. Returns (ok, [bad files]). Archive members have no per-file hash (checked at download)."""
    bad = []; ex = expected(mid)
    if job: job.begin_hash(sum(s for _, s, h in ex if h))
    for rel, size, sha in ex:
        p = MODELS + '/' + rel
        if not os.path.isfile(p) or (size is not None and os.path.getsize(p) != size) or (sha and sha256(p, job) != sha): bad.append(rel)
    if bad and delete_bad:
        for rel in bad:
            try: os.unlink(MODELS + '/' + rel)
            except OSError: pass
    return not bad, bad

# ---------------------------------------------------------------- install / adopt / remove
def install(mid, dry=False, force=False):
    global _critical, _cancel_pending
    r = REG[mid]
    print('%s: %s, %s, license %s' % (mid, CAT.get(mid, {}).get('name', mid), human(total_size(mid)), r['license']))
    if dry:
        for u in ([r['archive']['url']] if 'archive' in r else [f['url'] for f in r['files']]): print('  [dry-run] would download', u)
        return
    st = model_state(mid)
    if st == 'installed' and not force:
        print('  already installed: %s (run `voice-typing-models verify` to re-check, or remove it and install again)' % mid); return
    cur = read_status(mid)
    if cur and cur.get('status') in ACTIVE and cur.get('pid') != os.getpid() and _pid_is_ours(cur.get('pid', 0)):
        raise SystemExit('%s is already being downloaded (pid %s).' % (mid, cur['pid']))
    os.makedirs(STAGE + '/' + mid, exist_ok=True)
    signal.signal(signal.SIGTERM, _on_term)
    job = Job(mid)
    try:
        job.set('downloading')
        if st == 'missing' or force:
            free = shutil.disk_usage(MODELS).free; need = int(total_size(mid) * (2.3 if 'archive' in r else 1.05))
            if free < need: raise Fail('Not enough free disk space: this model needs about %s and %s is free.' % (human(need), human(free)))
        if st == 'unverified' and not force:
            job.set('verifying', 'Checking the files already on disk...')
            ok, bad = verify_files(mid, job, delete_bad=True)
            if ok: write_marker(mid); st = 'setup'
            else: print('  existing files failed the checksum, downloading again: %s' % ', '.join(bad)); st = 'missing'
        if st in ('missing',) or force:
            if 'archive' in r: fetch_archive(mid, job)
            else: fetch_files(mid, job)
            if not files_ok(mid): raise Fail('The files were not all installed correctly. Press Retry.')
            _critical = True; write_marker(mid); _critical = False
        shutil.rmtree(STAGE + '/' + mid, ignore_errors=True)
        if setup_needed(mid): print('  Model files downloaded. Complete the separate package/engine setup in Settings > Setup & uninstall.')
        else: print('  installed:', mid)
        clear_status(mid)
    except Cancelled:
        clear_status(mid); print('\n  cancelled: %s (what was downloaded is kept, so Download continues where it stopped)' % mid)
        raise SystemExit(130)
    except BaseException as e:
        if isinstance(e, (SystemExit, KeyboardInterrupt)): clear_status(mid); raise
        msg = friendly(e); write_status(mid, status='failed', msg=msg); print('  FAILED: %s' % msg, file=sys.stderr)
        if not isinstance(e, Fail): print('  (%r)' % (e,), file=sys.stderr)
        raise SystemExit(1)
    finally:
        _critical = False

def _adopt_legacy_part(final, staged):
    """An older version left <file>.part next to the final file: reuse it as the resume point."""
    if os.path.exists(final + '.part') and not os.path.exists(staged + '.part'):
        os.makedirs(os.path.dirname(staged), exist_ok=True)
        try: os.replace(final + '.part', staged + '.part')
        except OSError: pass

def fetch_files(mid, job):
    global _critical
    r = REG[mid]; staged = []
    for f in r['files']:
        final = '%s/%s' % (MODELS, f['dest']); st = '%s/%s/%s' % (STAGE, mid, f['dest'])
        _adopt_legacy_part(final, st)
        download(f['url'], st, f['sha256'], f['size'], os.path.basename(f['dest']), job)
        staged.append((st, final))
    _critical = True                                   # moving the verified files into place must not be interrupted
    try:
        for st, final in staged:
            os.makedirs(os.path.dirname(final), exist_ok=True); os.replace(st, final)
    finally: _critical = False
    if _cancel_pending: raise Cancelled()

def fetch_archive(mid, job):
    global _critical
    a = REG[mid]['archive']; tmp = '%s/%s/%s' % (STAGE, mid, os.path.basename(a['url']))
    download(a['url'], tmp, a['sha256'], a['size'], mid, job)
    job.set('installing', 'Unpacking...')
    xdir = '%s/%s/x' % (STAGE, mid); shutil.rmtree(xdir, ignore_errors=True)
    with tarfile.open(tmp, 'r:bz2') as tf: safe_extract(tf, xdir, a['dir'])
    new = '%s/%s' % (xdir, a['dir'])
    for n, s in (a.get('expect') or {}).items():
        p = '%s/%s' % (new, n)
        if not os.path.isfile(p) or os.path.getsize(p) != s: raise Fail('The unpacked model is incomplete (%s). Press Retry.' % n)
    if not a.get('expect') and not os.path.exists(new + '/tokens.txt'): raise Fail('The unpacked model is incomplete. Press Retry.')
    _critical = True
    try:
        final = '%s/%s/%s' % (MODELS, a['dest'], a['dir']); os.makedirs(os.path.dirname(final), exist_ok=True)
        old = final + '.old'; shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(final): os.replace(final, old)
        os.replace(new, final); shutil.rmtree(old, ignore_errors=True)
    finally: _critical = False
    if _cancel_pending: raise Cancelled()

def adopt(mid):
    """Models downloaded by an older version (or by hand) have no marker: check them once and mark them installed."""
    job = Job(mid); signal.signal(signal.SIGTERM, _on_term)
    try:
        job.set('verifying'); ok, bad = verify_files(mid, job, delete_bad=True)
        if ok: write_marker(mid); print('%-20s verified, marked installed' % mid)
        else: print('%-20s BAD (removed so it can be downloaded again): %s' % (mid, ', '.join(bad)))
        clear_status(mid); return ok
    except Cancelled: clear_status(mid); return False
    except BaseException: clear_status(mid); raise

def active_model():
    p = os.environ.get('GVT_CONFIG') or ((os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')) + '/gnome-voice-typing/config.json')
    try: return json.load(open(p)).get('model')
    except Exception: return None

def remove(mid, active=(), force=False):
    names = {a for a in active if a}
    cfg = active_model()
    if cfg: names.add(cfg)
    if mid in names and not force:
        print('%s is the model in use. Switch to another model first, then uninstall it.' % CAT.get(mid, {}).get('name', mid), file=sys.stderr); return False
    cur = read_status(mid)
    if cur and cur.get('status') in ACTIVE and _pid_is_ours(cur.get('pid', 0)):
        print('%s is being downloaded right now. Cancel the download first.' % mid, file=sys.stderr); return False
    try: os.unlink('%s/%s.json' % (MARK, mid))                    # marker first: from here on it no longer counts as installed
    except OSError: pass
    r = REG[mid]
    if 'archive' in r: paths = ['%s/%s/%s' % (MODELS, r['archive']['dest'], r['archive']['dir'])]
    else: paths = ['%s/%s' % (MODELS, f['dest']) for f in r['files']]
    for p in paths:
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else None
        for q in (p, p + '.part'):
            try: os.unlink(q)
            except OSError: pass
    if 'archive' not in r:
        d = os.path.dirname(paths[0])
        if d.startswith(MODELS + '/ct2/'): shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree('%s/%s' % (STAGE, mid), ignore_errors=True); clear_status(mid)
    print('removed', mid)
    return True

def cancel(mid):
    s = read_status(mid)
    if not s or s.get('status') not in ACTIVE: print('%s is not downloading.' % mid); return 0
    pid = s.get('pid', 0)
    if _pid_is_ours(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if not _pid_is_ours(pid): break
            time.sleep(0.1)
        else:
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
    clear_status(mid); print('cancelled', mid); return 0

# ---------------------------------------------------------------- system dependencies (first-launch check)
# The graphics-card engine is compiled on this machine and needs distro packages (cmake, glslc, Vulkan headers, ...).
# install.sh can skip them (--no-system-deps, no sudo); the app then runs on the CPU. `deps` tells the Settings panel
# what is missing; `deps install COMPONENT` installs only the selected item (pkexec for system changes).
DEPS_ID = '_deps'
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
DISMISS = XDG_CONFIG + '/gnome-voice-typing/deps-dismissed.json'
PACKAGE_PLAN = MODELS + '/.packages-plan.json'
def _sysdeps():
    try: return json.load(open(N + '/data/sysdeps.json'))
    except Exception: return {}
def pkg_manager():
    for pm, exe in (('dnf', 'dnf'), ('apt', 'apt-get'), ('pacman', 'pacman')):
        if shutil.which(exe): return pm
    return None
def _pkg_installed(q, pkg):
    try: return subprocess.run(q + [pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except Exception: return False
def vulkan_gpu():
    """True: a real Vulkan GPU (not llvmpipe). False: Vulkan works but only software. None: can't tell (vulkaninfo missing)."""
    if os.environ.get('GVT_FAKE_GPU') in ('0', '1'): return os.environ['GVT_FAKE_GPU'] == '1'
    if not shutil.which('vulkaninfo'): return None if glob_any('/dev/dri/renderD*') else False
    try: out = subprocess.run(['vulkaninfo', '--summary'], capture_output=True, text=True, timeout=15).stdout
    except Exception: return None
    return any(t in out for t in ('PHYSICAL_DEVICE_TYPE_DISCRETE_GPU', 'PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU', 'PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU'))
def glob_any(pat):
    import glob
    return bool(glob.glob(pat))
def engine_built(): return os.path.exists(N + '/gpu/libasrshim.so')
def ydotool_ok():
    if shutil.which('ydotoold'):
        try: r = subprocess.run(['ydotoold', '--help'], capture_output=True, text=True, timeout=5); return '--socket-path' in (r.stdout + r.stderr)
        except Exception: return False
    return os.access(N + '/ext/bin/ydotoold', os.X_OK)
UINPUT_RULE = '/etc/udev/rules.d/70-gnome-voice-typing-uinput.rules'; UINPUT_LOAD = '/etc/modules-load.d/gnome-voice-typing-uinput.conf'
def uinput_ok(): return os.access('/dev/uinput', os.W_OK)
def _uinput_script():
    rule = open(N + '/data/70-gnome-voice-typing-uinput.rules').read() if os.path.exists(N + '/data/70-gnome-voice-typing-uinput.rules') else \
        'KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"\n'
    return ("printf %%s %s > %s && chmod 644 %s && echo uinput > %s && modprobe uinput && udevadm control --reload && udevadm trigger --name-match=uinput --action=add"
            % (shlex.quote(rule), UINPUT_RULE, UINPUT_RULE, UINPUT_LOAD))
def _record_udev():
    p = N + '/install-state.json'
    try: d = json.load(open(p))
    except Exception: d = {}
    d['udev'] = {'rule': UINPUT_RULE, 'modules_load': UINPUT_LOAD}
    try: json.dump(d, open(p, 'w'), indent=1)
    except Exception: pass
def rnnoise_ok():
    import ctypes
    try: ctypes.CDLL('librnnoise.so.0'); return True
    except OSError: return False
def vad_pip_missing():
    """Python packages for the Silero voice detector that aren't importable in the app's environment (empty = all there)"""
    p = _sysdeps().get('pip', {}); mods = p.get('modules', []); pk = p.get('vad', [])
    py = N + '/venv/bin/python'
    if not os.path.exists(py): py = sys.executable
    out = []
    for m, k in zip(mods, pk):
        try: ok = subprocess.run([py, '-c', 'import ' + m], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode == 0
        except Exception: ok = False
        if not ok: out.append(k)
    return out
def deps_status():
    pm = pkg_manager(); spec = _sysdeps().get(pm or '', {}); q = spec.get('query')
    gpu = vulkan_gpu(); built = engine_built()
    miss_gpu = [] if (built or gpu is False or not q) else [p for p in spec.get('gpu', []) if not _pkg_installed(q, p)]
    miss_typ = [] if (ydotool_ok() or not q) else [p for p in spec.get('typing', []) if not _pkg_installed(q, p)]
    miss_noise = [] if (rnnoise_ok() or not q) else [p for p in spec.get('noise', []) if not _pkg_installed(q, p)]
    miss_pip = vad_pip_missing()
    missing = sorted(set(miss_gpu + miss_typ + miss_noise))
    perm = not uinput_ok()      # the virtual keyboard (/dev/uinput) is root-only: typing needs the one-time udev rule
    cmd = (['pkexec'] + spec['install'] + missing) if (missing and spec) else []
    if perm and shutil.which('pkexec'):
        cmd = ['pkexec', 'sh', '-c', ((shlex.join(spec['install'] + missing) + ' && ') if (missing and spec) else '') + _uinput_script()]
    try: dismissed = json.load(open(DISMISS)).get('missing') == missing + miss_pip + (['uinput'] if perm else [])
    except Exception: dismissed = False
    s = read_status(DEPS_ID)
    d = {'pm': pm, 'gpu': gpu, 'engine_built': built, 'missing_gpu': miss_gpu, 'missing_typing': miss_typ, 'missing_noise': miss_noise, 'missing_pip': miss_pip, 'missing_perm': perm,
            'missing': missing, 'total': len(missing) + len(miss_pip) + (1 if perm else 0),
            'pkexec': bool(shutil.which('pkexec')), 'command': cmd, 'command_text': ' '.join(cmd), 'manual': ('sudo ' + ' '.join(spec['install'] + missing)) if (missing and spec) else ('./install.sh (step 7)' if perm else ''),
            'needed': bool(missing) or bool(miss_pip) or perm or (gpu is not False and not built and not miss_gpu and bool(q)),
            'dismissed': dismissed, 'job': s}
    d['components'] = deps_components(d)
    d['total'] = len(d['components']); d['needed'] = bool(d['components'])
    return d


def deps_components(d):
    """Only missing packages are offered, based on actual installed state, not session flags."""
    py = N + '/venv/bin/python'
    plan = package_plan.load(PACKAGE_PLAN, py)
    d['plan_ready'] = plan is not None
    rows = []
    try: inventory = package_plan.python_inventory(py)
    except Exception:
        d['check_error'] = 'Could not read the app’s installed packages. Repair the Python environment with the main installer, then refresh this list.'
        return []
    try:
        pip_ready = subprocess.run([py, '-m', 'pip', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except OSError: pip_ready = False
    if not pip_ready:
        rows.append({'id': 'bootstrap:pip', 'name': 'pip', 'kind': 'bootstrap', 'installed': False, 'can_install': True,
                     'description': 'Package installer for the app’s private Python environment.', 'note': 'Installs only the bundled pip package. No download required.'})
    if plan:
        for item in plan['items']:
            if package_plan.installed(item, py, d['pm'], inventory): continue
            rows.append(dict(item, installed=False, can_install=(item['kind'] == 'python' or d['pkexec']),
                             note='Installs only this package. Supporting libraries have their own buttons.'))
    planned = {item['id'] for item in (plan or {}).get('items', [])}
    for package in d['missing']:
        if 'system:' + package not in planned:
            rows.append({'id': 'system:' + package, 'name': package, 'kind': 'system', 'installed': False, 'can_install': False,
                         'description': 'System package.', 'note': 'Refresh the package list to include its supporting libraries.'})
    for package in _sysdeps().get('pip', {}).get('vad', []):
        key = package_plan.canonical(package)
        if key not in inventory and 'python:' + key not in planned:
            rows.append({'id': 'python:' + key, 'name': package, 'kind': 'python', 'installed': False, 'can_install': False,
                         'description': 'Optional voice-detection package.', 'note': 'Refresh the package list to include all supporting libraries.'})
    if d['missing_perm']:
        rows.append({'id': 'permission', 'name': 'Typing permission', 'kind': 'permission', 'installed': False, 'can_install': d['pkexec'],
                     'description': 'Allows the virtual keyboard to type into applications.', 'note': 'One-time system rule; asks for your password.'})
    if d['gpu'] is not False and not d['engine_built']:
        rows.append({'id': 'engine', 'name': 'Graphics-card engine', 'kind': 'engine', 'installed': False, 'can_install': not d['missing_gpu'],
                     'description': 'Optional faster recognition using Vulkan.', 'note': 'Install the listed build packages first. No models or packages are installed by this build.'})
    return rows

def deps_install(component=None):
    if not component:
        print('Choose a package or component in Settings > Setup & uninstall, or use deps install COMPONENT. Run deps --json to list component IDs.', file=sys.stderr)
        return 2
    os.makedirs(STATUS, exist_ok=True)
    with open(STATUS + '/_deps.lock', 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another setup component is being installed. Wait for it to finish.', file=sys.stderr)
            return 1
        return _deps_install_one(component)


def _deps_install_one(component):
    signal.signal(signal.SIGTERM, _on_term)
    def st(status, msg, **extra):
        write_status(DEPS_ID, status=status, msg=msg, component=component, **extra)
        print('  ' + msg, flush=True)
    try:
        st('installing', 'Checking package requirements…', pct=0)
        py = N + '/venv/bin/python'
        if not os.path.exists(py): raise Fail('The app’s Python environment is missing. Run the main installer to repair it.')
        d = deps_status()
        if component == 'catalog':
            package_plan.resolve(PACKAGE_PLAN, py, d['pm'], d['missing'], _sysdeps().get('pip', {}).get('vad', []), lambda message: st('installing', message))
            st('done', 'Package list refreshed. Choose the packages you want to install.'); return 0
        if component == 'bootstrap:pip':
            st('installing', 'Installing the bundled pip package…', pct=100)
            package_plan.run([py, '-m', 'ensurepip'])
            st('done', 'pip installed.', pct=100); return 0
        if component.startswith(('system:', 'python:')):
            plan = package_plan.load(PACKAGE_PLAN, py)
            item = next((e for e in (plan or {}).get('items', []) if e['id'] == component), None)
            if item is None: raise Fail('Refresh the package list first. Each supporting package will get its own Install button.')
            if package_plan.installed(item, py, d['pm']):
                st('done', item['name'] + ' is already installed.', pct=100); return 0
            def progress(pct, got, total):
                message = ('Installing ' if pct == 100 else 'Downloading ') + item['name']
                if pct is not None: message += ' · ' + str(pct) + '%'
                st('installing' if pct == 100 else 'downloading', message, pct=pct, got=got, total=total)
            package_plan.install_one(item, py, d['pm'], progress)
            st('done', item['name'] + ' installed.', pct=100); return 0
        if component == 'permission':
            if not d['missing_perm']: st('done', 'Typing permission is already active.'); return 0
            if not d['pkexec']: raise Fail('A graphical password prompt (pkexec) is required to add the typing rule.')
            st('installing', 'Waiting for your password to add typing permission…')
            package_plan.run(['pkexec', 'sh', '-c', _uinput_script()])
            _record_udev()
            st('done', 'Typing permission added. Log out and back in if it is not active yet.'); return 0
        if component == 'engine':
            if engine_built(): st('done', 'The graphics-card engine is already built.'); return 0
            missing_tools = engine_tools_missing()
            if missing_tools or d['missing_gpu']: raise Fail('Install the listed build packages first: ' + ', '.join(d['missing_gpu'] or missing_tools))
            st('building', 'Building the graphics-card engine…')
            p = subprocess.Popen(['bash', N + '/scripts/build-gpu.sh', N], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL, start_new_session=True)
            try: out = p.communicate()[0]
            except BaseException:
                try: os.killpg(p.pid, signal.SIGTERM)
                except OSError: pass
                raise
            if p.returncode or not engine_built():
                print(out[-1500:], file=sys.stderr)
                raise Fail('Could not build the graphics-card engine. See ~/.cache/gnome-voice-typing/build/ for the build log.')
            st('done', 'Graphics-card engine built.'); return 0
        raise Fail('Unknown setup component. Reopen Settings and choose a listed item.')
    except Cancelled:
        st('failed', 'Cancelled.'); return 1
    except Exception as e:
        st('failed', str(e) if isinstance(e, (Fail, RuntimeError)) else friendly(e)); return 1

def deps_ack():
    """Persist OK without clearing missing-package reminders or active/failed jobs."""
    s = read_status(DEPS_ID)
    if s and s.get('status') == 'done':
        try: os.unlink(_sfile(DEPS_ID))
        except FileNotFoundError: pass

def deps_dismiss(on=True):
    if not on:
        try: os.unlink(DISMISS)
        except OSError: pass
        return
    os.makedirs(os.path.dirname(DISMISS), exist_ok=True)
    d = deps_status(); json.dump({'missing': d['missing'] + d['missing_pip'] + (['uinput'] if d['missing_perm'] else []), 't': time.strftime('%F %T')}, open(DISMISS, 'w'))

# ---------------------------------------------------------------- reporting
def entry_state(mid):
    ms = model_state(mid); s = read_status(mid)
    if s and s['status'] in ACTIVE: return s['status'], s
    if ms == 'installed':
        if s: clear_status(mid)
        return 'installed', None
    if s and s['status'] == 'failed': return 'failed', s
    return ms, s

def status_all():
    out = []
    for mid in CATORDER + [m for m in REG if m not in CATORDER]:
        if mid not in REG: continue
        state, s = entry_state(mid)
        e = dict(CAT.get(mid, {'id': mid, 'name': mid}), state=state, size=total_size(mid), size_h=human(total_size(mid)), license=REG[mid]['license'])
        if s: e.update(pct=s.get('pct', 0), got=s.get('got', 0), total=s.get('total', 0), msg=s.get('msg', ''))
        if state == 'setup': e['setup'] = setup_needed(mid)
        out.append(e)
    return out

def main(a):
    if not a or a[0] in ('-h', '--help', 'help'): print(__doc__); return 0
    cmd = a[0]; rest = []; opts = {}
    i = 1
    while i < len(a):
        x = a[i]
        if x == '--active' and i + 1 < len(a): opts.setdefault('active', []).append(a[i + 1]); i += 2; continue
        if x.startswith('--'): opts[x] = True
        else: rest.append(x)
        i += 1
    if cmd == 'deps':
        sub = rest[0] if rest else 'status'
        if sub == 'install': return deps_install(rest[1] if len(rest) == 2 else None)
        if sub == 'ack': deps_ack(); return 0
        if sub in ('dismiss', 'undismiss'): deps_dismiss(sub == 'dismiss'); return 0
        d = deps_status()
        if '--json' in opts: print(json.dumps(d)); return 0
        print('graphics card (Vulkan): %s   engine built: %s   package manager: %s' % ({True: 'yes', False: 'no', None: 'unknown'}[d['gpu']], 'yes' if d['engine_built'] else 'no', d['pm'] or 'unsupported'))
        if not d['needed']: print('nothing missing')
        print('missing for the graphics-card engine: %s' % (' '.join(d['missing_gpu']) or '-'))
        print('missing for typing: %s' % (' '.join(d['missing_typing']) or '-'))
        print('missing for noise suppression (optional): %s' % (' '.join(d['missing_noise']) or '-'))
        print('missing for better voice detection (optional, Python packages, no password needed): %s' % (' '.join(d['missing_pip']) or '-'))
        for item in d['components']:
            print('%-25s %-10s %s' % (item['id'], 'installed' if item['installed'] else 'available', item['description']))
        print('Install one item: voice-typing-models deps install COMPONENT'); return 0
    unknown = [m for m in rest if m not in REG]
    if unknown: print('unknown model id(s): %s\nknown: %s' % (', '.join(unknown), ', '.join(REG)), file=sys.stderr); return 2
    dry = '--dry-run' in opts; force = '--force' in opts
    if cmd == 'list':
        for e in status_all(): print('%-20s %-11s %-9s %-10s %s' % (e['id'], e['state'], human(e['size']), e['license'], e.get('name', '')))
    elif cmd == 'status':
        if '--json' in opts: print(json.dumps({'models': status_all(), 'free': shutil.disk_usage(MODELS if os.path.isdir(MODELS) else N).free}))
        else:
            for e in status_all(): print('%-20s %-11s %s' % (e['id'], e['state'], e.get('msg', '')))
    elif cmd == 'install':
        if not rest: print('usage: voice-typing-models install ID [ID...]', file=sys.stderr); return 2
        os.makedirs(MODELS, exist_ok=True)
        for m in rest: install(m, dry, force)
    elif cmd == 'remove':
        if not rest: print('usage: voice-typing-models remove ID [ID...]', file=sys.stderr); return 2
        return 0 if all([remove(m, opts.get('active', []), force) for m in rest]) else 3
    elif cmd == 'cancel':
        for m in rest: cancel(m)
    elif cmd == 'adopt':
        todo = rest or [m for m in REG if model_state(m) == 'unverified']
        for m in todo:
            if model_state(m) == 'unverified': adopt(m)
    elif cmd == 'verify':
        rc = 0
        for m in (rest or [x for x in REG if model_state(x) in ('installed', 'setup', 'unverified')]):
            if not files_ok(m): print('%-20s BAD missing or wrong-sized files' % m); rc = 1; continue
            ok, bad = verify_files(m)
            print('%-20s %s %s' % (m, 'OK ' if ok else 'BAD', ('OK' if 'archive' not in REG[m] else 'archive files present, sizes match (checksum was verified at download)') if ok else 'bad: ' + ', '.join(bad)))
            if ok and not read_marker(m): write_marker(m)
            rc |= (not ok)
        return rc
    elif cmd == 'licenses':
        for mid, r in REG.items(): print('%-20s %-10s %s' % (mid, r['license'], r['attribution']))
    else: print(__doc__); return 2
    return 0

if __name__ == '__main__':
    try: sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt: sys.exit(130)
