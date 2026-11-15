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

A model counts as installed only when EVERY file listed in the registry exists with the right size AND a marker
(models/.installed/<id>.json, written after the SHA-256 check) matches the registry. Downloads are staged in models/.dl/<id>/
(*.part, resumable) and moved into place only when complete, so a half-downloaded model never sits in the models folder.
"""
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

def ensure_pkg(mid, job):
    if REG[mid].get('needs') != 'faster-whisper' or importlib.util.find_spec('faster_whisper'): return
    job.set('installing', 'Installing the faster-whisper add-on (needed for this model)...')
    p = subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'faster-whisper'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode: raise Fail("Couldn't install the faster-whisper add-on (pip failed). Check your internet connection and press Retry.")
    importlib.invalidate_caches()

def engine_tools_missing():
    return [t for t in ('git', 'cmake', 'c++', 'glslc') if not shutil.which(t)]
def ensure_engine(mid, job):
    if setup_needed(mid) != 'engine': return
    script = N + '/scripts/build-gpu.sh'
    if not os.path.exists(script): raise Fail("The graphics-card engine isn't built and its build script is missing. Re-run install.sh.")
    job.set('building', 'Building the graphics-card engine (a few minutes)...')
    p = subprocess.Popen(['bash', script, N], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL, start_new_session=True)
    try: out = p.communicate()[0]
    except BaseException:
        try: os.killpg(p.pid, signal.SIGTERM)
        except OSError: pass
        raise
    if p.returncode or not os.path.exists(N + '/gpu/libasrshim.so'):
        print(out[-1500:], file=sys.stderr)
        raise Fail("Couldn't build the graphics-card engine (see the log from voice-typing-models install %s). Re-run install.sh to set up the build tools." % mid)

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
        if setup_needed(mid) == 'engine':
            miss = engine_tools_missing()
            if miss: raise Fail("The graphics-card engine isn't built yet and its build tools are missing (%s). Open Settings and choose System packages, or install them and press Retry." % ', '.join(miss))
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
        ensure_pkg(mid, job); ensure_engine(mid, job)
        print('  installed:', mid)
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
