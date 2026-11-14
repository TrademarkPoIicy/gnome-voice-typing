"""gnome-voice-typing daemon: warm speech model, continuous phrase-by-phrase dictation, typed into the focused window.
States: idle (hidden) -> listening <-> paused -> idle(close). Controlled over a unix socket by bin/dictate."""
import glob, os, sys, json, socket, threading, queue, time, subprocess, re, signal, datetime, collections, shutil
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = 'gnome-voice-typing'
CFG = {'backend': 'vk', 'model': 'parakeet-gpu', 'sounds': True, 'end_silence': 0.7, 'auto_pause_seconds': 60,
       'auto_close_paused_seconds': 300, 'ydotool_key_delay_ms': 6}
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
XDG_STATE = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
CFGFILE = os.environ.get('GVT_CONFIG') or (XDG_CONFIG + '/' + APP + '/config.json')       # user settings (model, mic, pause time, ...)
try: CFG.update(json.load(open(CFGFILE)))
except Exception: pass
RT = os.environ.get('XDG_RUNTIME_DIR') or ('/tmp/%s-%d' % (APP, os.getuid()))
os.makedirs(RT, mode=0o700, exist_ok=True)
CATALOG = json.load(open(N + '/data/models.json'))

def persist_cfg(**kw):
    try: c = json.load(open(CFGFILE))
    except Exception: c = {}
    c.update(kw); tmp = CFGFILE + '.tmp'
    os.makedirs(os.path.dirname(CFGFILE), exist_ok=True)
    json.dump(c, open(tmp, 'w'), indent=2); os.replace(tmp, CFGFILE); CFG.update(kw)

# ---------------- GPU (Vulkan) backend: gpu/backend.py (whisper.cpp / parakeet.cpp on the Radeon, model stays resident in VRAM) ----------------
VKMAP = {'parakeet-gpu': 'parakeet', 'small.en-gpu': 'small.en', 'medium.en-q5-gpu': 'medium.en-q5_0', 'large-v3-turbo-gpu': 'large-v3-turbo'}
VKFILES = {'parakeet-gpu': 'ggml-parakeet-tdt-0.6b-v3-f16.bin', 'small.en-gpu': 'ggml-small.en.bin', 'medium.en-q5-gpu': 'ggml-medium.en-q5_0.bin', 'large-v3-turbo-gpu': 'ggml-large-v3-turbo-q5_0.bin'}
FALLBACK = ('sherpa', 'moonshine-base')          # used automatically if Vulkan/GPU model fails to load
# device choice (config 'device': 'gpu' | 'cpu'); a model's device is derived from its backend ('vk' = graphics card)
EQUIV = {'parakeet-gpu': 'parakeet', 'small.en-gpu': 'small.en', 'medium.en-q5-gpu': 'medium.en', 'large-v3-turbo-gpu': 'distil-large-v3'}
EQUIV.update({v: k for k, v in list(EQUIV.items())})            # equivalent model on the other device
DEFAULTS = {'gpu': 'parakeet-gpu', 'cpu': 'parakeet'}           # CPU default: Parakeet (0.0% WER on dictation-length clips, ~0.26 s) beats Moonshine base (6.7%, ~0.16 s)
DEVNAME = {'gpu': 'Graphics card', 'cpu': 'Processor'}
def dev_of(e): return 'gpu' if e and e.get('backend') == 'vk' else 'cpu'
ASR_LOCK = threading.Lock()                      # ONE lock around every transcribe/load/close (Vulkan contexts are not thread-safe)

class VkAsr:
    def __init__(self, mid):
        sys.path.insert(0, N + '/gpu'); import backend
        self.name = 'vulkan-gpu-' + mid; self.mid = mid; self.m = backend.load(VKMAP[mid])
    def transcribe(self, audio): return self.m.transcribe(audio)       # callers hold ASR_LOCK
    def close(self):
        if self.m is not None: self.m.close(); self.m = None             # callers hold ASR_LOCK

def cat_entry(mid): return next((e for e in CATALOG if e['id'] == mid), None)
def load_model(ent, strict=False):
    """-> (asr object, effective model id). strict: raise on failure (used by live switching, which then keeps the old model).
    Non-strict fallback works both ways: GPU model fails -> CPU default; CPU model fails -> GPU default. (Saved choice is never rewritten.)"""
    import asr
    if ent['backend'] == 'vk':
        try:
            with ASR_LOCK:
                m = VkAsr(ent['id']); asr.warm(m)
            return m, ent['id']
        except Exception as e:
            if strict: raise
            fb = cat_entry(DEFAULTS['cpu'])
            if not (fb and installed(fb)): fb = cat_entry(FALLBACK[1])
            log('GPU BACKEND FAILED (%r) -> falling back to %s on CPU' % (e, fb['id']))
            return asr.load(fb['backend'], fb['id'], strict=True), fb['id']
    try:
        return asr.load(ent['backend'], ent['id'], strict=True), ent['id']
    except Exception as e:
        if strict: raise
        g = cat_entry(DEFAULTS['gpu'])
        if g and installed(g):
            log('CPU MODEL FAILED (%r) -> falling back to %s on GPU' % (e, g['id']))
            try:
                with ASR_LOCK:
                    m = VkAsr(g['id']); asr.warm(m)
                return m, g['id']
            except Exception as e2: log('GPU fallback failed too', repr(e2))
        return asr.load(ent['backend'], ent['id'], strict=False), ent['id']

def installed(e):
    if e['backend'] == 'vk':
        return e['id'] in VKMAP and os.path.exists(N + '/models/ggml/' + VKFILES[e['id']]) and os.path.exists(N + '/gpu/libasrshim.so')
    if e['backend'] == 'sherpa':
        import asr
        return os.path.exists(N + '/models/sherpa/' + asr.Sherpa.DIRS[e['id']] + '/tokens.txt')
    return os.path.exists(N + '/models/ct2/' + e['id'] + '/model.bin')

def list_mics():
    out = [{'id': '', 'name': 'System default'}]
    try:
        r = subprocess.run(['pactl', '--format=json', 'list', 'sources'], capture_output=True, text=True, timeout=3)
        for d in json.loads(r.stdout):
            if d.get('monitor_source') or '.monitor' in d['name']: continue
            out.append({'id': d['name'], 'name': d.get('description') or d['name']})
    except Exception: pass
    return out
SOCK = RT + '/gnome-voice-typing.sock'
YSOCK = RT + '/gnome-voice-typing-ydotool.sock'
OVDIR = RT + '/gnome-voice-typing-overlay'
LOG = os.environ.get('GVT_LOG') or (XDG_STATE + '/' + APP + '/daemon.log')
os.makedirs(os.path.dirname(LOG), exist_ok=True)
EXTBIN = N + '/ext/bin'                                            # ydotool 1.0.4 built from source by install.sh, only when the distro's is too old
YDOTOOL = (EXTBIN + '/ydotool') if os.path.exists(EXTBIN + '/ydotool') else (shutil.which('ydotool') or '/usr/bin/ydotool')    # ydotool is AGPL-3.0 and is not bundled
YDOTOOLD = (EXTBIN + '/ydotoold') if os.path.exists(EXTBIN + '/ydotoold') else (shutil.which('ydotoold') or '/usr/bin/ydotoold')
RATE = 16000; CHUNK = RATE // 10 * 2     # 100 ms of s16 mono
DRY = bool(os.environ.get('GVT_DRY_RUN'))      # developer option: log what would be typed instead of typing it

def log(*a):
    with open(LOG, 'a') as f:
        f.write(datetime.datetime.now().strftime('%F %T.%f')[:-3] + ' ' + ' '.join(str(x) for x in a) + '\n')

# ---------------- overlay (state file watched by the GNOME Shell extension) ----------------
_ovlock = threading.Lock()
EXTRA = {'model': '', 'device': '', 'loading': '', 'switched': ''}
def ov(state, level=0.0, text='', busy=False):
    if not os.path.exists(OVDIR + '/ready'): return
    with _ovlock:
        try:
            tmp = OVDIR + '/state.tmp'
            with open(OVDIR + '/state.json', 'w') as f:
                json.dump(dict({'state': state, 'level': round(level, 3), 'text': text, 'busy': busy}, **EXTRA), f)
        except Exception: pass

def chime(name):
    return   # sounds are played by the overlay extension via bin/chime.sh on state changes

# ---------------- typing ----------------
class Typer:
    CHARMAP = (('\u2014', ' - '), ('\u2013', '-'), ('\u2018', "'"), ('\u2019', "'"), ('\u201c', '"'), ('\u201d', '"'),
               ('\u2026', '...'), ('\u00a0', ' '), ('\u202f', ' '))
    def __init__(self):
        self.yd = None; self.portal = None
    def ydotool_ready(self):
        if not os.access('/dev/uinput', os.W_OK): return False
        if self.yd and self.yd.poll() is None and os.path.exists(YSOCK): return True
        try: os.unlink(YSOCK)
        except Exception: pass
        env = dict(os.environ, YDOTOOL_SOCKET=YSOCK)
        self.yd = subprocess.Popen([YDOTOOLD, '-p', YSOCK], env=env, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=open(LOG, 'a'), stderr=subprocess.STDOUT)
        for _ in range(60):
            if os.path.exists(YSOCK): return True
            time.sleep(0.05)
        return False
    TERMINALS = ('ptyxis', 'gnome-terminal', 'org.gnome.terminal', 'org.gnome.console', 'kgx', 'konsole', 'xterm', 'alacritty', 'kitty', 'foot',
                 'wezterm', 'tilix', 'terminator', 'xfce4-terminal', 'terminology', 'urxvt', 'blackbox', 'ghostty', 'guake', 'yakuake',
                 'qterminal', 'lxterminal', 'sakura', 'cool-retro-term')
    def _focus_is_terminal(self):
        # the overlay extension writes the focused window's WM_CLASS / app id to focus.json; without it (extension not loaded yet) ask AT-SPI via focusbox.py
        names = ''
        try:
            o = json.load(open(OVDIR + '/focus.json')); names = ' '.join(str(o.get(k, '')) for k in ('wm_class', 'app_id', 'sandbox_id')).lower()
        except Exception:
            try:
                r = subprocess.run(['/usr/bin/python3', N + '/bin/focusbox.py', '0.3'], capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
                names = str(json.loads(r.stdout.decode() or '{}').get('key', '')).split(':')[0].lower()
            except Exception: names = ''
        return any(t in names for t in self.TERMINALS)
    def _clip(self, args, timeout=1.5):
        # wl-copy can block (GNOME has no data-control protocol, so it needs keyboard focus a popup/popover will not give it): detached, short timeout, never raises
        try:
            p = subprocess.Popen(['wl-copy'] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception: return False
        try: return p.wait(timeout=timeout) == 0
        except Exception:
            try: p.kill(); p.wait(1)
            except Exception: pass
            time.sleep(0.2)     # a stuck wl-copy may be holding keyboard focus on its helper surface: let it go back to the app before typing again
            return False
    @staticmethod
    def _ascii_approx(seg):
        import unicodedata
        return ''.join(c for c in unicodedata.normalize('NFKD', seg) if ord(c) < 128)
    def _yd(self, *args, inp=None):
        env = dict(os.environ, YDOTOOL_SOCKET=YSOCK)
        subprocess.run([YDOTOOL] + list(args), input=inp, env=env, check=True, timeout=120)
    def type(self, text):
        for a, b in self.CHARMAP: text = text.replace(a, b)
        if DRY: log('DRY RUN - would type: %r' % text); return 'dry'
        if self.ydotool_ready():
            d = str(CFG.get('ydotool_key_delay_ms', 6)); old = None; term = None; clipbad = False
            for seg in re.findall(r'[\x00-\x7f]+|[^\x00-\x7f]+', text):
                if seg[0] < '\x80':
                    self._yd('type', '-d', d, '-H', '2', '-f', '-', inp=seg.encode())
                else:    # accents/CJK: ydotool is US-layout; use clipboard + Ctrl+V (Ctrl+Shift+V in terminals); never let a stuck clipboard stop the phrase
                    ok = False
                    try:
                        if old is None:
                            try:
                                r = subprocess.run(['wl-paste', '--no-newline', '--type', 'text/plain'], capture_output=True, timeout=1, stdin=subprocess.DEVNULL)
                                old = r.stdout if r.returncode == 0 else b''
                            except Exception: old = False        # unreadable: paste anyway, but never "restore"
                        if not clipbad and self._clip(['--', seg]):
                            time.sleep(0.2)
                            if term is None: term = self._focus_is_terminal()
                            if term: self._yd('key', '29:1', '42:1', '47:1', '47:0', '42:0', '29:0')
                            else: self._yd('key', '29:1', '47:1', '47:0', '29:0')
                            time.sleep(0.3); ok = True
                    except Exception as e: log('unicode paste failed: %r' % e)
                    if not ok:
                        clipbad = True      # paste route is stuck for this phrase: don't wait 1.5 s again for every remaining chunk
                        asc = self._ascii_approx(seg); log('unicode paste unavailable here; typing an ASCII approximation (%d chars)' % len(seg))
                        if asc: self._yd('type', '-d', d, '-H', '2', '-f', '-', inp=asc.encode())
            if old:
                self._clip(['--', old.decode('utf-8', 'replace')])
            elif old == b'':
                self._clip(['--clear'])
            return 'ydotool'
        # fallback: portal (shows GNOME's Remote Desktop indicator); persistent helper, 12 s startup cap
        if self.portal is None or self.portal.poll() is not None:
            p = subprocess.Popen(['/usr/bin/python3', N + '/bin/inject.py', '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, 'a'))
            st = {}; t = threading.Thread(target=lambda: st.__setitem__('l', p.stdout.readline().decode().strip()), daemon=True); t.start(); t.join(12)
            if st.get('l') != 'READY': p.kill(); raise RuntimeError('portal injector: ' + str(st.get('l') or 'timeout'))
            self.portal = p
        self.portal.stdin.write((json.dumps({'text': text}) + '\n').encode()); self.portal.stdin.flush()
        self.portal.stdout.readline()
        return 'portal'
    def backspace(self, n):
        if DRY: log('DRY RUN - would backspace %d' % n); return True
        if n <= 0 or not self.ydotool_ready(): return False
        args = ['key']
        for _ in range(min(n, 400)): args += ['14:1', '14:0']
        self._yd(*args); return True
    def close(self):
        if self.portal and self.portal.poll() is None:
            try: self.portal.stdin.close(); self.portal.wait(3)
            except Exception: self.portal.kill()
        self.portal = None

def postprocess(text):
    t = text.strip()
    t = re.sub(r'[,.]?\s*\bnew paragraph\b[.,]?\s*', '\n\n', t, flags=re.I)
    t = re.sub(r'[,.]?\s*\bnew line\b[.,]?\s*', '\n', t, flags=re.I)
    return t

HALLUC = {'you', 'thank you', 'thanks for watching', 'bye', 'the end', 'thanks', 'okay', 'so'}
CMD_PAUSE = {'stop listening', 'pause dictation', 'pause listening', 'stop dictation', 'pause', 'stop voice typing'}
CMD_CLOSE = {'close dictation', 'close voice typing', 'exit dictation', 'quit dictation'}
CMD_DELETE = {'delete that', 'scratch that', 'undo that', 'delete last phrase'}
def norm(t): return re.sub(r'[^a-z ]', '', t.lower()).strip()

class Session:
    def __init__(self, **kw):
        self.prefix = ''; self.enter = False; self.src = None; self.oneshot = False; self.dry = False
        self.__dict__.update(kw); self.first = True

class Daemon:
    def __init__(self):
        self.state = 'idle'; self.lock = threading.RLock(); self.stop = threading.Event()
        self.thread = None; self.q = queue.Queue(); self.pending = 0; self.asr = None; self.asr_err = None
        self.level = 0.0; self.err_until = 0; self.err_text = ''; self.paused_at = 0
        self.typer = Typer(); self.last_typed = ''; self.sess = None
        self.cur_id = CFG['model']; self.switch = {'status': 'idle', 'target': '', 'msg': ''}; self.switched_until = 0
        self.mseq = 0; self.dev_note = ''; self.set_extra_model(self.cur_id); self.write_models()
        threading.Thread(target=self.load_asr, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.housekeeping, daemon=True).start()

    def set_extra_model(self, mid):
        EXTRA['model'] = self.name_of(mid); EXTRA['device'] = 'GPU' if dev_of(cat_entry(mid)) == 'gpu' else 'CPU'

    def name_of(self, mid):
        for e in CATALOG:
            if e['id'] == mid: return e['name']
        return mid

    def write_models(self):
        if not os.path.isdir(OVDIR): return
        self.mseq += 1
        d = {'cli': N + '/bin/voice-typing', 'current': self.cur_id, 'current_name': self.name_of(self.cur_id),
             'models': [dict(e, device=dev_of(e)) for e in CATALOG if installed(e)], 'equiv': EQUIV, 'defaults': DEFAULTS, 'note': self.dev_note,
             'settings': {'auto_pause_seconds': CFG['auto_pause_seconds'], 'mic': CFG.get('mic', ''), 'sounds': bool(CFG.get('sounds', True)),
                          'device': dev_of(cat_entry(self.cur_id)), 'mics': list_mics()},
             'switch': self.switch, 'seq': self.mseq}
        try:
            with open(OVDIR + '/models.tmp', 'w') as f: json.dump(d, f)
            os.replace(OVDIR + '/models.tmp', OVDIR + '/models.json')
        except Exception as e: log('write_models failed', repr(e))

    def apply(self, msg):
        ch = {}
        if 'auto_pause_seconds' in msg: ch['auto_pause_seconds'] = int(msg['auto_pause_seconds'])
        if 'mic' in msg: ch['mic'] = str(msg['mic'])
        if 'sounds' in msg: ch['sounds'] = bool(msg['sounds'])
        mid = msg.get('model'); dev = msg.get('device')
        if dev in ('gpu', 'cpu') and not mid and dev_of(cat_entry(self.cur_id)) != dev:   # device only: equivalent model if there is one, else the device default
            eq = cat_entry(EQUIV.get(self.cur_id))
            mid = eq['id'] if (eq and dev_of(eq) == dev and installed(eq)) else DEFAULTS[dev]
        if dev in ('gpu', 'cpu') and (not mid or mid == self.cur_id) and dev_of(cat_entry(self.cur_id)) == dev: ch['device'] = dev
        if ch: persist_cfg(**ch); log('settings saved', ch)
        if mid and mid != self.cur_id:
            if self.switch['status'] == 'loading': return
            ent = next((e for e in CATALOG if e['id'] == mid), None)
            if not ent: self.switch = {'status': 'error', 'target': mid, 'msg': 'Unknown model'}
            else:
                self.switch = {'status': 'loading', 'target': mid, 'msg': ''}
                EXTRA['loading'] = ent['name']
                threading.Thread(target=self.swap, args=(ent,), daemon=True).start()
        self.write_models(); self.show()

    def swap(self, ent):
        import asr
        log('model switch: loading %s in background (current %s keeps working)' % (ent['id'], self.cur_id))
        try:
            t = time.time(); new, _ = load_model(ent, strict=True)
            with ASR_LOCK:
                if ent['backend'] != 'vk': asr.warm(new)
                old = self.asr; self.asr = new; self.cur_id = ent['id']
                if old is not None and hasattr(old, 'close'): old.close()      # load-new-then-close-old: GPU memory of the old model is freed only after the switch
                del old
            persist_cfg(backend=ent['backend'], model=ent['id'], device=dev_of(ent)); self.dev_note = ''
            self.set_extra_model(ent['id']); EXTRA['loading'] = ''; EXTRA['switched'] = ent['name']
            self.switch = {'status': 'ready', 'target': ent['id'], 'msg': 'Ready'}
            self.switched_until = time.time() + 2.2
            log('model switch done: %s in %.1fs' % (ent['id'], time.time() - t))
            threading.Timer(2.3, self._clear_switched).start()
        except Exception as e:
            log('model switch FAILED, keeping %s: %r' % (self.cur_id, e))
            EXTRA['loading'] = ''
            cur = cat_entry(self.cur_id)
            self.dev_note = ('%s unavailable, using %s' % (DEVNAME[dev_of(ent)], DEVNAME[dev_of(cur)].lower())) if dev_of(ent) != dev_of(cur) else ''
            self.switch = {'status': 'error', 'target': ent['id'], 'msg': 'Could not load %s - keeping %s' % (ent['name'], self.name_of(self.cur_id))}
        self.write_models(); self.show()

    def _clear_switched(self):
        EXTRA['switched'] = ''; self.show()

    def load_asr(self):
        try:
            import asr; t = time.time()
            ent = cat_entry(CFG['model']) or {'backend': CFG['backend'], 'id': CFG['model']}
            a, eff = load_model(ent)                       # falls back across devices by itself (GPU->CPU or CPU->GPU), logged only
            if a.name.startswith('ct2') and ent['backend'] != 'ct2' and eff == ent['id']: eff = 'small.en'
            if eff != ent['id'] and dev_of(cat_entry(eff)) != dev_of(ent):
                self.dev_note = '%s unavailable, using %s' % (DEVNAME[dev_of(ent)], DEVNAME[dev_of(cat_entry(eff))].lower())
            lt = time.time() - t
            with ASR_LOCK: wt = asr.warm(a); wt2 = asr.warm(a)
            self.asr = a
            if eff != self.cur_id:
                self.cur_id = eff; self.set_extra_model(eff); self.write_models(); self.show()   # config.json untouched: next start retries the chosen device
            log('model ready: backend=%s model=%s load=%.1fs warmup=%.2fs/%.2fs%s' % (a.name, eff, lt, wt, wt2, '' if eff == CFG['model'] else ' (FALLBACK from %s)' % CFG['model']))
        except Exception as e:
            self.asr_err = repr(e); log('MODEL ERROR', repr(e))

    # --- state/overlay
    def show(self, state=None):
        s = state or self.state
        busy = self.pending > 0
        if s == 'listening' and time.time() < self.err_until: ov('error', text=self.err_text)
        elif s == 'listening' and self.asr is None and not self.asr_err: ov('starting', busy=True)
        elif s in ('listening', 'paused', 'starting'): ov(s, self.level, busy=busy)
        else: ov('hidden')

    def error(self, text, secs=3):
        self.err_text = text; self.err_until = time.time() + secs; log('ERROR shown:', text)

    # --- commands
    def handle(self, msg):
        c = msg.get('cmd'); log('cmd', c, '(state %s)' % self.state)
        if c in ('toggle', 'start', 'pause', 'resume') and self.switch['status'] == 'loading':
            log('ignored (model is loading)'); return json.dumps({'state': self.state, 'loading': True})
        if c == 'models':
            self.write_models(); return json.dumps({'current': self.cur_id, 'models': [dict(e, installed=installed(e)) for e in CATALOG], 'switch': self.switch, 'settings': {'auto_pause_seconds': CFG['auto_pause_seconds'], 'mic': CFG.get('mic', ''), 'device': dev_of(cat_entry(self.cur_id))}})
        if c == 'apply': self.apply(msg); return json.dumps({'ok': True, 'switch': self.switch})
        with self.lock:
            if c == 'toggle':
                if self.state == 'idle': self.start(Session())
                elif self.state == 'listening': self.pause()
                else: self.start(self.sess or Session())
            elif c == 'start': self.start(Session())
            elif c == 'pause': self.pause()
            elif c == 'resume': self.start(self.sess or Session())
            elif c == 'close': self.close()
            elif c == 'testwav' and DRY:
                self.start(Session(src=msg['path'], prefix=msg.get('prefix', ''), enter=bool(msg.get('enter')), oneshot=True))
            elif c == 'quit':
                self.close()
                try: os.unlink(SOCK)
                except Exception: pass
                os._exit(0)
        return json.dumps({'state': self.state, 'asr': self.asr.name if self.asr else None, 'asr_err': self.asr_err})

    def start(self, sess):
        if self.state == 'listening': return
        self.sess = sess; self.stop.clear(); self.state = 'listening'; self.err_until = 0
        chime('service-login.oga'); self.show(); threading.Thread(target=self.write_models, daemon=True).start()
        self.thread = threading.Thread(target=self.rec_loop, args=(sess,), daemon=True); self.thread.start()

    def _halt(self):
        self.stop.set()
        t = self.thread
        if t and t is not threading.current_thread(): t.join(5)

    def pause(self):
        if self.state != 'listening': return
        self._halt(); self.state = 'paused'; self.paused_at = time.time(); chime('device-removed.oga'); self.show()

    def close(self):
        if self.state == 'idle': return
        self._halt(); self.state = 'idle'; self.sess = None; self.typer.close(); self.show()

    def housekeeping(self):
        while True:
            time.sleep(2)
            with self.lock:
                if self.state == 'paused' and time.time() - self.paused_at > CFG['auto_close_paused_seconds']:
                    log('auto-close after paused timeout'); self.close()

    # --- audio
    def rec_loop(self, sess):
        cmd = ['pw-record', '--raw', '--rate', str(RATE), '--channels', '1', '--format', 's16'] + (['--target', CFG.get('mic')] if CFG.get('mic') else []) + ['-']
        if sess.src: cmd = ['ffmpeg', '-v', 'quiet', '-re', '-i', sess.src, '-af', 'adelay=500,apad=pad_dur=2.5', '-f', 's16le', '-ar', '16000', '-ac', '1', '-']
        try: rec = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.error('Microphone unavailable'); log('recorder failed', repr(e)); self.state = 'paused'; self.show(); return
        end_chunks = max(2, int(round(CFG['end_silence'] * 10)))
        preroll = collections.deque(maxlen=3); phrase = []; in_speech = False; silent = 0; loud_n = 0
        calib = []; floor = None; thr = 300; last_speech = time.time(); started = time.time(); eof = False
        def submit(chunks, trail):
            if loud_n < 3: return                      # blip, ignore
            audio = np.frombuffer(b''.join(chunks[:len(chunks) - max(0, trail - 2)] if trail > 2 else chunks), dtype=np.int16).astype(np.float32) / 32768.0
            self.pending += 1; self.q.put((audio, sess)); log('phrase queued %.1fs' % (len(audio) / RATE))
        while not self.stop.is_set():
            d = rec.stdout.read(CHUNK)
            if not d: eof = True; break
            x = np.frombuffer(d, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            self.level = min(1.0, (max(0.0, rms - (floor or 0.0)) / 3000.0) ** 0.5); self.show('listening')
            if floor is None:
                calib.append(rms); preroll.append(d)
                if len(calib) >= 4: floor = float(min(calib)); thr = min(max(250.0, floor * 3.0), 600.0)
                continue
            loud = rms > thr
            if not in_speech:
                preroll.append(d)
                if loud: in_speech = True; phrase = list(preroll); silent = 0; loud_n = 1; last_speech = time.time()
                elif not loud: floor = 0.95 * floor + 0.05 * rms; thr = min(max(250.0, floor * 3.0), 600.0)
            else:
                phrase.append(d)
                if loud: silent = 0; loud_n += 1; last_speech = time.time()
                else: silent += 1
                if silent >= end_chunks or len(phrase) > 250:
                    submit(phrase, silent); in_speech = False; phrase = []; preroll.clear(); loud_n = 0; silent = 0
            if not in_speech and CFG['auto_pause_seconds'] > 0 and time.time() - last_speech > CFG['auto_pause_seconds'] and not sess.src:
                log('auto-pause (no speech)'); threading.Thread(target=lambda: self.handle({'cmd': 'pause'}), daemon=True).start(); break
        try: rec.terminate(); rec.wait(2)
        except Exception: rec.kill()
        if in_speech and phrase: submit(phrase, silent)
        self.level = 0.0
        if sess.oneshot and eof:       # test feed finished: drain, press Enter if asked, close
            self.q.join()
            if sess.enter and not DRY: self.typer.type('\n')
            elif sess.enter: log('DRY RUN - would press Enter')
            with self.lock:
                self.state = 'idle'; self.sess = None; self.show(); self.typer.close()

    # --- transcription + typing
    def worker(self):
        while True:
            audio, sess = self.q.get()
            try: self.phrase(audio, sess)
            except Exception as e:
                log('phrase error', repr(e)); self.error('Could not type that phrase')
            finally:
                self.pending -= 1; self.q.task_done()

    def phrase(self, audio, sess):
        while self.asr is None and not self.asr_err: time.sleep(0.05)
        if self.asr is None: self.error('Speech model failed to load'); return
        t = time.time()
        with ASR_LOCK: text = self.asr.transcribe(audio).strip()      # self.asr read inside the lock: a switch may close the old model
        tt = time.time() - t
        dur = len(audio) / RATE
        log('transcribed %.1fs audio in %.2fs (%d chars)%s' % (dur, tt, len(text), (': %r' % text) if DRY else ''))   # typed text is never logged (except in the developer dry-run mode)
        n = norm(text)
        if not n or (dur < 2.0 and n in HALLUC): return
        if n in CMD_PAUSE: log('voice command: pause'); threading.Thread(target=lambda: self.handle({'cmd': 'pause'}), daemon=True).start(); return
        if n in CMD_CLOSE: log('voice command: close'); threading.Thread(target=lambda: self.handle({'cmd': 'close'}), daemon=True).start(); return
        if n in CMD_DELETE:
            if self.typer.backspace(len(self.last_typed)): self.last_typed = ''
            return
        out = postprocess(text)
        if sess.first and sess.prefix: out = sess.prefix + out
        sess.first = False
        t = time.time(); how = self.typer.type(out + ' '); self.last_typed = out + ' '
        log('typed via %s in %.2fs (%d chars)' % (how, time.time() - t, len(out)))

D = Daemon()

def serve():
    try: os.unlink(SOCK)
    except Exception: pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(SOCK); os.chmod(SOCK, 0o600); s.listen(8)
    log('daemon up (pid %d, backend %s)' % (os.getpid(), CFG['backend']))
    while True:
        c, _ = s.accept()
        try:
            msg = json.loads(c.makefile().readline() or '{}')
            c.sendall((D.handle(msg) + '\n').encode())
        except Exception as e:
            log('ipc error', repr(e))
            try: c.sendall(b'{"error":"1"}\n')
            except Exception: pass
        finally: c.close()

def _term(*a):
    D.close()
    try: os.unlink(SOCK)
    except Exception: pass
    os._exit(0)
signal.signal(signal.SIGTERM, _term)
if '--show-start' in sys.argv: ov('starting', busy=True)
serve()
