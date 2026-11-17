"""gnome-voice-typing daemon: warm speech model, continuous phrase-by-phrase dictation, typed into the focused window.
States: idle (hidden) -> listening <-> paused -> idle(close). Controlled over a unix socket by bin/dictate."""
import glob, os, sys, json, socket, threading, queue, time, subprocess, re, signal, datetime, collections, shutil
import devlog
devlog.install_exception_hooks('daemon')
devlog.start('daemon startup')
import select
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audiofx
import customization
N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = 'gnome-voice-typing'
CFG = {'backend': 'vk', 'model': 'parakeet-gpu', 'sounds': True, 'end_silence': 0.7, 'auto_pause_seconds': 60,
       'auto_close_paused_seconds': 300, 'ydotool_key_delay_ms': 6,
       'streaming': 'auto',            # 'auto' (type while you talk whenever the model keeps up, adaptive pace) | true (always stream, never give up) | false (type at the end of each phrase)
       'stream_interval': 0.4,         # fastest pace of partial passes in seconds; backs off automatically when a pass is slow
       'noise_mode': 'auto',           # 'auto' = adaptive noise floor | 'manual' = use the calibrated room noise (noise_floor) as well
       'silence_threshold': 0.5,       # speech sensitivity 0..1 (higher picks up quieter speech)
       'noise_suppression': True,      # neural noise suppression (RNNoise, else GTCRN) before voice detection and speech recognition
       'noise_floor': 0.0}             # set by 'voice-typing calibrate'
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
XDG_STATE = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
CFGFILE = os.environ.get('GVT_CONFIG') or (XDG_CONFIG + '/' + APP + '/config.json')       # user settings (model, mic, pause time, ...)
try: CFG.update(json.load(open(CFGFILE)))
except Exception: pass
RT = os.environ.get('XDG_RUNTIME_DIR') or ('/tmp/%s-%d' % (APP, os.getuid()))
os.makedirs(RT, mode=0o700, exist_ok=True)
CATALOG = json.load(open(N + '/data/models.json'))

def persist_cfg(**kw):
    customization.update_config(CFGFILE, kw)
    CFG.update(kw)

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

def pick_installed():
    """The model to load: the saved choice if it is installed, else a default, else any installed model; None = nothing installed."""
    ent = cat_entry(CFG.get('model'))
    if ent and installed(ent): return ent
    for mid in (DEFAULTS.get(CFG.get('device') or 'gpu'), DEFAULTS['gpu'], DEFAULTS['cpu']):
        e = cat_entry(mid)
        if e and installed(e): return e
    return next((e for e in CATALOG if installed(e)), None)

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
    fields = {'message': ' '.join(str(x) for x in a)}
    if sys.exc_info()[0] is not None: fields['traceback'] = __import__('traceback').format_exc()
    devlog.event('daemon', 'MESSAGE', **fields)
    if DRY:
        with open(LOG, 'a') as f:
            f.write(datetime.datetime.now().isoformat() + ' ' + ' '.join(str(x) for x in a) + '\n')

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
class NotReady(RuntimeError): pass
class Typer:
    CHARMAP = (('\u2014', ' - '), ('\u2013', '-'), ('\u2018', "'"), ('\u2019', "'"), ('\u201c', '"'), ('\u201d', '"'),
               ('\u2026', '...'), ('\u00a0', ' '), ('\u202f', ' '))
    def __init__(self):
        self.yd = None; self.portal = None
    def ydotool_ready(self):
        if not os.access('/dev/uinput', os.W_OK): return False
        if not (os.access(YDOTOOL, os.X_OK) and os.access(YDOTOOLD, os.X_OK)): return False     # ydotool not installed
        if self.yd and self.yd.poll() is None and os.path.exists(YSOCK): return True
        try: os.unlink(YSOCK)
        except Exception: pass
        env = dict(os.environ, YDOTOOL_SOCKET=YSOCK)
        try: self.yd = subprocess.Popen([YDOTOOLD, '-p', YSOCK], env=env, start_new_session=True,
                                        stdin=subprocess.DEVNULL, stdout=open(LOG, 'a'), stderr=subprocess.STDOUT)
        except OSError as e: log('ydotoold could not start (%r)' % e); return False
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
        devlog.event('typing', 'DELIVERY', arguments=list(args))
        subprocess.run([YDOTOOL] + list(args), input=inp, env=env, check=True, timeout=120)
        devlog.event('typing', 'DELIVERY COMPLETE')
    def type(self, text):
        devlog.event('typing', 'TEXT REQUEST', text=text)
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
        # No GNOME remote-control portal by default (it pops a screen-sharing prompt). Opt back in with GVT_PORTAL=1.
        if os.environ.get('GVT_PORTAL') != '1':
            raise NotReady('Typing is not set up: open Settings > Setup & uninstall and press Install')
        if self.portal is None or self.portal.poll() is not None:
            p = subprocess.Popen(['/usr/bin/python3', N + '/bin/inject.py', '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(LOG, 'a'))
            st = {}; t = threading.Thread(target=lambda: st.__setitem__('l', p.stdout.readline().decode().strip()), daemon=True); t.start(); t.join(12)
            if st.get('l') != 'READY': p.kill(); raise RuntimeError('portal injector: ' + str(st.get('l') or 'timeout'))
            self.portal = p
        self.portal.stdin.write((json.dumps({'text': text}) + '\n').encode()); self.portal.stdin.flush()
        self.portal.stdout.readline()
        return 'portal'
    def backspace(self, n):
        devlog.event('typing', 'BACKSPACE', characters=n)
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
    return customization.format_text(text, CFG)

HOLD_LAST = {'new'}                  # a trailing word that may become a voice command is held back while streaming
HALLUC = {'you', 'thank you', 'thanks for watching', 'bye', 'the end', 'thanks', 'okay', 'so'}
CMD_PAUSE = {'stop listening', 'pause dictation', 'pause listening', 'stop dictation', 'pause', 'stop voice typing'}
CMD_CLOSE = {'close dictation', 'close voice typing', 'exit dictation', 'quit dictation'}
CMD_DELETE = {'delete that', 'scratch that', 'undo that', 'delete last phrase'}
def norm(t): return re.sub(r'[^a-z ]', '', t.lower()).strip()

def read_audio(rec, stopped=None):
    """Read one PCM frame without letting a stalled PipeWire stream hang a worker."""
    data = bytearray(); deadline = time.monotonic() + 3
    while len(data) < CHUNK:
        if stopped and stopped(): return b''
        if time.monotonic() >= deadline: raise RuntimeError('Microphone stream stopped sending audio')
        ready, _, _ = select.select([rec.stdout], [], [], 0.1)
        if not ready: continue
        part = os.read(rec.stdout.fileno(), CHUNK - len(data))
        if not part: return bytes(data)
        data.extend(part)
    return bytes(data)


def stop_recorder(rec):
    if rec is None: return
    try:
        if rec.poll() is None: rec.terminate()
        rec.wait(timeout=1)
    except subprocess.TimeoutExpired:
        rec.kill(); rec.wait(timeout=1)
    except OSError: pass


class Session:
    def __init__(self, **kw):
        self.prefix = ''; self.enter = False; self.src = None; self.oneshot = False; self.dry = False
        self.__dict__.update(kw); self.first = True

class Phrase:
    """Streaming state of one phrase (utterance): words typed so far, last hypothesis, pacing of the partial passes."""
    def __init__(self, sess):
        self.sess = sess; self.words = []; self.words_n = []; self.chunks = []; self.prev_n = []
        self.off = False; self.done = False; self.inflight = False; self.next_at = 0.0; self.last_n = 0
        self.passes = 0; self.t_sum = 0.0; self.interval = 0.4

class Daemon:
    def __init__(self):
        self.state = 'idle'; self.lock = threading.RLock(); self.stop = threading.Event()
        self.recorder = None; self.monitor_recorder = None; self.monitor_epoch = 0; self.input_warning = ''
        self.thread = None; self.q = queue.Queue(); self.pending = 0; self.asr = None; self.asr_err = None
        self.level = 0.0; self.err_until = 0; self.err_text = ''; self.paused_at = 0
        self.typer = Typer(); self.last_typed = ''; self.sess = None
        self.partial = None; self.stream_block_until = 0; self.meter_until = 0; self.mon = None; self.rms = 0.0; self.floor = 0.0; self.calib = {'status': 'idle'}
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
             'settings': {**customization.settings(CFG), 'auto_pause_seconds': CFG['auto_pause_seconds'], 'mic': CFG.get('mic', ''), 'sounds': bool(CFG.get('sounds', True)), 'follow_field': bool(CFG.get('follow_field', False)),
                          'noise_mode': CFG.get('noise_mode', 'auto'), 'silence_threshold': CFG.get('silence_threshold', 0.5), 'noise_suppression': bool(CFG.get('noise_suppression', True)),
                          'noise_floor': CFG.get('noise_floor', 0.0), 'noise_floor_raw': CFG.get('noise_floor_raw', 0.0), 'streaming': CFG.get('streaming', 'auto'),
                          'device': dev_of(cat_entry(self.cur_id)), 'mics': list_mics()},
             'logging_error': devlog.last_error, 'switch': self.switch, 'last_error': getattr(self, 'last_error', None), 'seq': self.mseq}
        try:
            with open(OVDIR + '/models.tmp', 'w') as f: json.dump(d, f)
            os.replace(OVDIR + '/models.tmp', OVDIR + '/models.json')
        except Exception as e: log('write_models failed', repr(e))

    def apply(self, msg):
        ch = customization.validate(msg)
        if msg.get('clear_error') is True: self.last_error = None
        if 'auto_pause_seconds' in msg: ch['auto_pause_seconds'] = int(msg['auto_pause_seconds'])
        if 'mic' in msg: ch['mic'] = str(msg['mic'])
        if 'sounds' in msg: ch['sounds'] = bool(msg['sounds'])
        if 'follow_field' in msg: ch['follow_field'] = bool(msg['follow_field'])
        if msg.get('noise_mode') in ('auto', 'manual'): ch['noise_mode'] = msg['noise_mode']
        if 'silence_threshold' in msg:
            try: ch['silence_threshold'] = round(max(0.0, min(1.0, float(msg['silence_threshold']))), 2)
            except Exception: pass
        if 'noise_suppression' in msg: ch['noise_suppression'] = bool(msg['noise_suppression'])
        if 'streaming' in msg: ch['streaming'] = msg['streaming'] if msg['streaming'] in (True, False, 'auto') else 'auto'
        mid = msg.get('model'); dev = msg.get('device')
        if dev in ('gpu', 'cpu') and not mid and dev_of(cat_entry(self.cur_id)) != dev:   # device only: equivalent model if there is one, else the device default
            eq = cat_entry(EQUIV.get(self.cur_id))
            mid = eq['id'] if (eq and dev_of(eq) == dev and installed(eq)) else DEFAULTS[dev]
        if dev in ('gpu', 'cpu') and (not mid or mid == self.cur_id) and dev_of(cat_entry(self.cur_id)) == dev: ch['device'] = dev
        mic_changed = 'mic' in ch and ch['mic'] != CFG.get('mic', '')
        audio_changed = mic_changed or ('noise_suppression' in ch and ch['noise_suppression'] != CFG.get('noise_suppression', True))
        if mic_changed: ch.update(noise_floor=0.0, noise_floor_raw=0.0)
        if audio_changed and self.calib.get('status') == 'running':
            raise ValueError('Wait for microphone calibration to finish, then save the new microphone.')
        if ch:
            if ch.get('developer_logging') is False: devlog.event('daemon', 'LOGGING DISABLED')
            was_logging = CFG.get('developer_logging') is True
            persist_cfg(**ch)
            if ch.get('developer_logging') is True and not was_logging: devlog.start('logging enabled')
            devlog.event('daemon', 'SETTINGS CHANGED', changes=ch)
        if audio_changed:
            with self.lock:
                self._stop_monitor()
                self.input_warning = ''
                if self.state == 'listening':
                    session = self.sess or Session()
                    self.pause(); self.start(session)
                else: self.meter()
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
            self.switch = {'status': 'error', 'target': ent['id'], 'msg': 'Could not load %s. Still using %s. Try a smaller model or Processor.' % (ent['name'], self.name_of(self.cur_id))}
        self.write_models(); self.show()

    def _clear_switched(self):
        EXTRA['switched'] = ''; self.show()

    def load_asr(self):
        try:
            import asr
            ent = pick_installed()
            if ent is None:                                # fresh install: no model yet. The bar offers 'Install model'; load the first one that appears
                EXTRA['no_model'] = True; EXTRA['model'] = ''; EXTRA['device'] = ''; self.write_models(); self.show()
                log('no speech model installed: waiting for one (Settings > Speech model > Download)')
                while ent is None: time.sleep(2); ent = pick_installed()
                EXTRA['no_model'] = False; EXTRA['loading'] = ent['name']; self.show()
                log('model installed: loading', ent['id'])
            if ent['id'] != CFG.get('model'): persist_cfg(backend=ent['backend'], model=ent['id'], device=dev_of(ent))
            t = time.time()
            a, eff = load_model(ent)                       # falls back across devices by itself (GPU->CPU or CPU->GPU), logged only
            if a.name.startswith('ct2') and ent['backend'] != 'ct2' and eff == ent['id']: eff = 'small.en'
            if eff != ent['id'] and dev_of(cat_entry(eff)) != dev_of(ent):
                self.dev_note = '%s unavailable, using %s' % (DEVNAME[dev_of(ent)], DEVNAME[dev_of(cat_entry(eff))].lower())
            lt = time.time() - t
            with ASR_LOCK: wt = asr.warm(a); wt2 = asr.warm(a)
            self.asr = a; EXTRA['loading'] = ''
            if eff != self.cur_id:
                self.cur_id = eff; self.set_extra_model(eff); self.write_models(); self.show()   # config.json untouched: next start retries the chosen device
            log('model ready: backend=%s model=%s load=%.1fs warmup=%.2fs/%.2fs%s' % (a.name, eff, lt, wt, wt2, '' if eff == CFG['model'] else ' (FALLBACK from %s)' % CFG['model']))
        except Exception as e:
            EXTRA['loading'] = ''; self.asr_err = repr(e); log('MODEL ERROR', repr(e))
            self.error('Speech model failed to load', detail='Open Settings > Speech model and try a smaller installed model or Processor. Check Settings > Setup & uninstall for missing packages.'); self.show()

    # --- state/overlay
    def show(self, state=None):
        s = state or self.state
        busy = self.pending > 0
        if EXTRA.get('no_model') and s in ('listening', 'paused', 'starting'): ov('paused', text='No speech model yet')
        elif s in ('listening', 'paused') and time.time() < self.err_until: ov('error', text=self.err_text)
        elif s == 'listening' and self.input_warning: ov('error', text=self.input_warning)
        elif s == 'listening' and self.asr is None and not self.asr_err: ov('starting', busy=True)
        elif s in ('listening', 'paused', 'starting'): ov(s, self.level, busy=busy)
        else: ov('hidden')

    def error(self, text, secs=8, detail=''):
        self.err_text = text; self.err_until = time.time() + secs; log('ERROR shown:', text)
        self.last_error = {'summary': text, 'detail': detail, 'time': time.strftime('%H:%M')}
        self.write_models()


    # --- commands
    def handle(self, msg):
        c = msg.get('cmd'); devlog.event('daemon', 'COMMAND', command=msg, state=self.state)
        if c in ('toggle', 'start', 'pause', 'resume') and self.switch['status'] == 'loading':
            log('ignored (model is loading)'); return json.dumps({'state': self.state, 'loading': True})
        if c == 'open-settings':
            if self.state == 'listening': self.pause()
            if self.state == 'idle': self.state = 'paused'
            EXTRA['settings_request'] = time.time(); self.write_models(); self.show()
            return json.dumps({'ok': True})
        if c == 'preferences': return json.dumps({'settings': customization.settings(CFG)})
        if c == 'models':
            self.write_models(); return json.dumps({'current': self.cur_id, 'models': [dict(e, installed=installed(e)) for e in CATALOG], 'switch': self.switch, 'settings': {'auto_pause_seconds': CFG['auto_pause_seconds'], 'mic': CFG.get('mic', ''), 'device': dev_of(cat_entry(self.cur_id))}})
        if c == 'apply':
            try: self.apply(msg)
            except (ValueError, OSError) as e: return json.dumps({'error': 'Settings were not saved: ' + str(e)})
            return json.dumps({'ok': True, 'switch': self.switch, 'preferences': customization.settings(CFG)})
        if c == 'calibrate': return json.dumps(self.calibrate())
        if c == 'meter': self.meter(); return json.dumps({'state': self.state, 'rms': round(self.rms, 1), 'floor': round(self.floor, 1)})
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
        r = {'state': self.state, 'asr': self.asr.name if self.asr else None, 'asr_err': self.asr_err}
        if c == 'status':
            r.update(level=round(self.level, 3), rms=round(self.rms, 1), floor=round(self.floor, 1), noise_mode=CFG.get('noise_mode', 'auto'), noise_floor=CFG.get('noise_floor', 0.0),
                     noise_floor_raw=CFG.get('noise_floor_raw', 0.0), silence_threshold=CFG.get('silence_threshold', 0.5), noise_suppression=bool(CFG.get('noise_suppression', True)),
                     streaming=CFG.get('streaming', 'auto'), calibrate=self.calib)
        return json.dumps(r)

    def start(self, sess):
        if self.state == 'listening': return
        if self.calib.get('status') == 'running':
            self.error('Microphone calibration running', detail='Wait for calibration to finish, then resume dictation.'); self.show(); return
        if self.thread and self.thread.is_alive():
            self._halt()
            if self.thread.is_alive():
                self.error('Microphone is still stopping', detail='Wait a moment and try again.'); self.show(); return
        self._stop_monitor()
        self.input_warning = ''
        if self.state == 'idle': devlog.start('dictation opened')
        devlog.event('daemon', 'LISTENING', previous_state=self.state, microphone=CFG.get('mic', ''), model=self.cur_id)
        self.sess = sess; self.stop.clear(); self.state = 'listening'; self.err_until = 0
        chime('service-login.oga'); self.show(); threading.Thread(target=self.write_models, daemon=True).start()
        self.thread = threading.Thread(target=self.rec_loop, args=(sess,), daemon=True); self.thread.start()

    def _halt(self):
        self.stop.set()
        stop_recorder(self.recorder)
        t = self.thread
        if t and t is not threading.current_thread(): t.join(5)

    def pause(self):
        if self.state != 'listening': return
        devlog.event('daemon', 'PAUSED')
        self._halt(); self.state = 'paused'; self.paused_at = time.time(); chime('device-removed.oga'); self.show()

    def close(self):
        if self.state == 'idle': return
        devlog.event('daemon', 'DICTATION CLOSED')
        self._halt(); self.state = 'idle'; self.sess = None; self.typer.close(); self.show()

    def housekeeping(self):
        while True:
            time.sleep(2)
            with self.lock:
                if self.state == 'paused' and time.time() - self.paused_at > CFG['auto_close_paused_seconds']:
                    log('auto-close after paused timeout'); self.close()

    # --- audio
    def _mic_cmd(self):
        return ['pw-record', '--raw', '--rate', str(RATE), '--channels', '1', '--format', 's16'] + (['--target', CFG.get('mic')] if CFG.get('mic') else []) + ['-']

    def _frontend(self):
        """(denoiser or None, silero vad) for one listening session / calibration, per the current settings"""
        dn = None
        if CFG.get('noise_suppression', True):
            dn = audiofx.Denoiser(CFG.get('noise_engine', 'auto'))
            if dn.eng is None: log('noise suppression unavailable (%s): continuing without it' % dn.err.strip()); dn = None
        vad = audiofx.Vad()
        if not vad.ok: log('silero VAD unavailable (%s): using the adaptive energy gate' % vad.err)
        return dn, vad

    @staticmethod
    def _meter_lv(v): return round(min(1.0, (max(0.0, v) / 6000.0) ** 0.5), 3)

    def _thresholds(self, dn):
        """-> (speech start prob, speech continue prob, energy multiplier, minimum energy threshold). silence_threshold is a SENSITIVITY 0..1 (higher = picks up quieter speech)."""
        try: s = max(0.0, min(1.0, float(CFG.get('silence_threshold', 0.5))))
        except Exception: s = 0.5
        return 0.75 - 0.5 * s, 0.60 - 0.5 * s, 4.0 - 3.0 * s, (40.0 if dn else 150.0)

    def rec_loop(self, sess):
        try:
            self._record_session(sess)
        except Exception as error:
            log('recording failed', repr(error))
            if not self.stop.is_set():
                self.state = 'paused'; self.paused_at = time.time()
                self.error('Microphone audio stopped', detail=str(error) + '. Check the microphone in Settings > Microphone & noise, then resume.')
                self.show()
        finally:
            stop_recorder(self.recorder)
            if self.recorder: self.recorder.stdout.close()
            self.recorder = None

    def _record_session(self, sess):
        cmd = self._mic_cmd()
        if sess.src: cmd = ['ffmpeg', '-v', 'quiet', '-re', '-i', sess.src, '-af', 'adelay=500,apad=pad_dur=2.5', '-f', 's16le', '-ar', '16000', '-ac', '1', '-']
        devlog.event('audio', 'RECORDER START', arguments=cmd)
        try:
            rec = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.recorder = rec
            devlog.capture_lines(rec.stderr, 'audio', 'RECORDER OUTPUT')
        except Exception as e:
            self.error('Microphone unavailable', detail='Open Settings > Microphone & noise and select a connected microphone. Check that it is not muted in system Sound settings, then try again.'); log('recorder failed', repr(e)); self.state = 'paused'; self.show(); return
        end_chunks = max(2, int(round(CFG['end_silence'] * 10)))
        dn, vad = self._frontend()
        log('listening: noise suppression=%s, vad=%s, noise mode=%s, streaming=%s' % (dn.name if dn else 'off', 'silero' if vad.ok else 'energy', CFG.get('noise_mode', 'auto'), CFG.get('streaming', 'auto')))
        fl = audiofx.Floor(); preroll = collections.deque(maxlen=4); phrase = []; in_speech = False; silent = 0; loud_n = 0
        last_speech = time.time(); eof = False; ph = None; n_chunks = 0; silent_input_since = time.monotonic()
        def submit(chunks, trail, ph):
            if loud_n < 3: return                      # blip, ignore
            audio = np.frombuffer(b''.join(chunks[:len(chunks) - max(0, trail - 2)] if trail > 2 else chunks), dtype=np.int16).astype(np.float32) / 32768.0
            if ph is not None:
                ph.done = True
                if self.partial is not None and self.partial[0] is ph: self.partial = None
            self.pending += 1; self.q.put((audio, sess, ph)); log('phrase queued %.1fs' % (len(audio) / RATE))
        while not self.stop.is_set():
            d = read_audio(rec, self.stop.is_set)
            if not d: eof = True; break
            d = d[:len(d) // 2 * 2]
            if not d: continue
            d_raw = d
            raw_samples = np.frombuffer(d_raw, dtype=np.int16).astype(np.float32)
            raw_rms = float(np.sqrt(np.mean(raw_samples * raw_samples))) if len(raw_samples) else 0.0
            if raw_rms > 0:
                silent_input_since = time.monotonic()
                if self.input_warning:
                    log('microphone signal recovered'); self.input_warning = ''
            elif time.monotonic() - silent_input_since > 5 and not sess.src and not self.input_warning:
                self.input_warning = 'No microphone signal'
                self.last_error = {'summary': self.input_warning, 'detail': 'The selected microphone is sending silence. Check mute and input volume, or select System default in Microphone & noise.', 'time': time.strftime('%H:%M')}
                log('microphone sending digital silence', CFG.get('mic', '')); self.write_models()
            if dn: d = dn.process(d)
            x = np.frombuffer(d, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            n_chunks += 1; now = time.time()
            p_start, p_cont, mult, min_thr = self._thresholds(dn)
            p = vad.prob(d) if vad.ok else None
            manual = CFG.get('noise_mode', 'auto') == 'manual'
            ref = float(CFG.get('noise_floor') or 0.0) if manual and CFG.get('noise_floor') else (fl.v if fl.v is not None else rms)   # manual: the calibrated room noise; auto: the tracked floor
            if manual: mult = 1.0 + 1.5 * (1.0 - (4.0 - mult) / 3.0)          # manual gate: 1.0x (most sensitive) .. 2.5x (least) the calibrated room noise, as Silero already does the main work
            e_thr = max(min_thr, ref * mult)
            if p is not None: loud = p >= (p_cont if in_speech else p_start)
            else: loud = rms > e_thr and n_chunks > 3
            if manual and p is not None: loud = loud and rms > e_thr          # manual mode: Silero AND the energy gate must agree
            fl.update(rms, loud or in_speech)
            self.rms = rms; self.floor = fl.v or 0.0; self.level = min(1.0, (max(0.0, rms - self.floor) / 3000.0) ** 0.5)
            EXTRA['mic'] = self._meter_lv(rms); EXTRA['floor_lv'] = self._meter_lv(manual and float(CFG.get('noise_floor') or 0.0) or self.floor); EXTRA['speech'] = bool(loud)
            if n_chunks % 10 == 0 or bool(loud) != getattr(self, '_logged_speech', None):
                devlog.event('audio', 'VOICE DETECTION', raw_rms=raw_rms, microphone=CFG.get('mic', ''), rms=rms, floor=self.floor, threshold=e_thr, probability=p, speech=bool(loud), in_phrase=bool(in_speech))
                self._logged_speech = bool(loud)
            self.show('listening')
            d_asr = d if CFG.get('noise_suppression_asr', True) else d_raw      # the recognizer hears the denoised audio unless noise_suppression_asr is false (voice detection always uses the denoised audio)
            if not in_speech:
                preroll.append(d_asr)
                if loud:
                    in_speech = True; phrase = list(preroll); silent = 0; loud_n = 1; last_speech = now
                    ph = Phrase(sess); mode = self._stream_mode(); ph.off = (mode == 'off') or (mode == 'auto' and now < self.stream_block_until)
            else:
                phrase.append(d_asr)
                if loud: silent = 0; loud_n += 1; last_speech = now
                else: silent += 1
                if silent >= end_chunks or (len(phrase) > 120 and silent >= 2) or len(phrase) > 250:    # a long phrase is cut at the first short pause after 12 s (hard cap 25 s)
                    submit(phrase, silent, ph); in_speech = False; phrase = []; preroll.clear(); loud_n = 0; silent = 0; ph = None
                elif ph is not None and not ph.off and loud_n >= 3: self._maybe_partial(ph, phrase, now)
            if not in_speech and CFG['auto_pause_seconds'] > 0 and now - last_speech > CFG['auto_pause_seconds'] and not sess.src:
                log('auto-pause (no speech)'); threading.Thread(target=lambda: self.handle({'cmd': 'pause'}), daemon=True).start(); break
        try: rec.terminate(); rec.wait(2)
        except Exception: rec.kill()
        if in_speech and phrase: submit(phrase, silent, ph)
        if dn: dn.close()
        self.level = 0.0; EXTRA['mic'] = 0.0; EXTRA['speech'] = False
        if eof and not sess.src and not self.stop.is_set():
            self.state = 'paused'
            self.error('Microphone disconnected', detail='Reconnect the microphone or select another one in Settings > Microphone & noise, then resume dictation.')
            self.show()
        if sess.oneshot and eof:       # test feed finished: drain, press Enter if asked, close
            self.q.join()
            if sess.enter and not DRY: self.typer.type('\n')
            elif sess.enter: log('DRY RUN - would press Enter')
            with self.lock:
                self.state = 'idle'; self.sess = None; self.show(); self.typer.close()

    # --- microphone monitor (live level meter for the settings panel while not listening) and room-noise calibration
    def _stop_monitor(self):
        self.monitor_epoch += 1
        self.meter_until = 0
        stop_recorder(self.monitor_recorder)
        if self.mon and self.mon is not threading.current_thread(): self.mon.join(4)

    def meter(self):
        self.meter_until = time.time() + 6
        if self.state != 'listening' and not (self.mon and self.mon.is_alive()) and self.calib.get('status') != 'running':
            self.mon = threading.Thread(target=self._monitor, daemon=True); self.mon.start()

    def _monitor(self):
        epoch = self.monitor_epoch
        try:
            rec = subprocess.Popen(self._mic_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.monitor_recorder = rec
        except Exception as e: log('meter: recorder failed', repr(e)); return
        dn = None
        try:
            dn, vad = self._frontend(); fl = audiofx.Floor(); n = 0
            while epoch == self.monitor_epoch and time.time() < self.meter_until and self.state != 'listening' and self.calib.get('status') != 'running':
                d = read_audio(rec, lambda: epoch != self.monitor_epoch or time.time() >= self.meter_until or self.state == 'listening' or self.calib.get('status') == 'running')
                if not d: break
                d = d[:len(d) // 2 * 2]
                if dn: d = dn.process(d)
                x = np.frombuffer(d, dtype=np.int16).astype(np.float32); rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0; n += 1
                p = vad.prob(d) if vad.ok else None; fl.update(rms, False)
                self.rms = rms; self.floor = fl.v or 0.0
                EXTRA['mic'] = self._meter_lv(rms); EXTRA['floor_lv'] = self._meter_lv(float(CFG.get('noise_floor') or 0.0) if CFG.get('noise_mode') == 'manual' and CFG.get('noise_floor') else self.floor)
                EXTRA['speech'] = bool(p is not None and p >= 0.5) if n > 3 else False
                self.level = min(1.0, (rms / 3000.0) ** 0.5)
                if self.state != 'listening': self.show()
        except Exception as error:
            log('microphone meter failed', repr(error))
        finally:
            stop_recorder(rec); rec.stdout.close()
            if self.monitor_recorder is rec: self.monitor_recorder = None
            if dn: dn.close()
            if self.state != 'listening': self.level = 0.0; EXTRA['mic'] = 0.0; EXTRA['speech'] = False; self.show()

    def calibrate(self):
        if self.state == 'listening': return {'error': 'listening', 'calibrate': self.calib}
        if self.calib.get('status') == 'running': return {'calibrate': self.calib}
        self.calib = {'status': 'running'}; EXTRA['calib'] = 'running'
        threading.Thread(target=self._calibrate, daemon=True).start()
        return {'calibrate': self.calib}

    def _calibrate(self):
        """Measure the room for 2 s through the same front end the dictation uses and save the floor (noise_floor, after suppression) and noise_floor_raw."""
        self._stop_monitor()
        raw = []; proc = []; rec = None; dn = None
        try:
            rec = subprocess.Popen(self._mic_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            dn, _ = self._frontend(); t0 = time.time()
            for i in range(24):
                d = read_audio(rec)
                if not d: break
                d = d[:len(d) // 2 * 2]; x = np.frombuffer(d, dtype=np.int16).astype(np.float32); r0 = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
                if dn: d = dn.process(d)
                y = np.frombuffer(d, dtype=np.int16).astype(np.float32); r1 = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
                if i >= 4: raw.append(r0); proc.append(r1)           # skip the first 0.4 s: the stream and the denoiser are still starting
                EXTRA['mic'] = self._meter_lv(r1); self.show()
            if len(proc) < 8 or max(raw, default=0) == 0: raise RuntimeError('Microphone gave no audio. Check mute/input volume or choose another microphone.')
            fl = max(1.0, round(float(np.percentile(proc, 90)), 1)); flr = round(float(np.percentile(raw, 90)), 1)      # 90th percentile of the quiet room: an upper bound of its noise
            persist_cfg(noise_floor=fl, noise_floor_raw=flr, noise_peak=round(float(np.max(proc)), 1))
            self.calib = {'status': 'done', 'floor': fl, 'raw': flr}; log('calibrated room noise: floor %.1f (raw %.1f) over %.1fs' % (fl, flr, time.time() - t0))
        except Exception as e:
            self.calib = {'status': 'error', 'msg': str(e)}; log('calibration failed', repr(e))
        finally:
            if rec:
                try: rec.terminate(); rec.wait(2)
                except Exception: rec.kill()
            if dn: dn.close()
            EXTRA['calib'] = self.calib.get('status', ''); EXTRA['mic'] = 0.0
            self.write_models(); self.show()
            threading.Timer(4.0, self._clear_calib).start()
    def _clear_calib(self):
        if self.calib.get('status') != 'running': EXTRA['calib'] = ''; self.show()

    # --- streaming (incremental typing): partial passes over the growing phrase, local agreement, adaptive pacing
    def _stream_mode(self):
        if customization.whole_phrase_required(CFG): return 'off'
        m = CFG.get('streaming', 'auto')
        if m is False or str(m).lower() in ('false', 'off', '0'): return 'off'
        if m is True or str(m).lower() in ('true', 'on', '1'): return 'on'
        return 'auto'

    def _maybe_partial(self, ph, phrase, now):
        if ph.inflight or ph.done or now < ph.next_at or len(phrase) < 10 or len(phrase) - ph.last_n < 2 or self.asr is None: return
        ph.inflight = True; ph.last_n = len(phrase); self.partial = (ph, b''.join(phrase))

    @staticmethod
    def _cmd_prefix(full):
        w = full.split()
        return (not w) or any(c.split()[:len(w)] == w for c in (CMD_PAUSE | CMD_CLOSE | CMD_DELETE))

    def do_partial(self, ph, pcm):
        if ph.done or self.asr is None: return
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t = time.time()
        with ASR_LOCK: text = self.asr.transcribe(audio).strip()
        t1 = time.time(); tt = t1 - t; dur = len(audio) / RATE
        devlog.event('recognition', 'PARTIAL', text=text, audio_seconds=dur, processing_seconds=tt, model=self.cur_id)
        mode = self._stream_mode(); base = float(CFG.get('stream_interval', 0.4)); mx = 4.0 if mode == 'on' else 3.0
        ph.passes += 1; ph.t_sum += tt; ph.interval = min(mx, max(base, 2.0 * tt)); ph.next_at = t + ph.interval      # at most ~50 % duty: back off as the pass gets slower
        if mode == 'auto' and 2.0 * tt > mx:        # even the slowest allowed pace (a pass every 3 s) would take more than half of the time: this model cannot keep up
            ph.off = True; log('streaming off for this phrase: partial pass took %.2fs on %.1fs of audio' % (tt, dur))
            if dur < 2.5: self.stream_block_until = time.time() + 60; log('model too slow to stream: whole-phrase mode for the next 60 s')
        if DRY: log('partial #%d %.1fs audio in %.2fs (next in %.2fs): %r' % (ph.passes, dur, tt, ph.interval, text))
        if ph.done: return
        self.stream_commit(ph, text, dur)

    def stream_commit(self, ph, text, dur):
        hyp = text.split(); nt = [norm(w) for w in hyp]; prev = ph.prev_n; ph.prev_n = nt
        k = 0
        while k < min(len(prev), len(nt)) and prev[k] == nt[k]: k += 1          # words two consecutive hypotheses agree on
        n = len(ph.words)
        if ph.words_n != nt[:n] or k <= n: return                                # a revised typed prefix, or nothing new yet: wait for the next pass
        if n == 0:
            full = ' '.join(w for w in nt if w)
            if self._cmd_prefix(full) or (dur < 2.0 and full in HALLUC): return    # could still turn into "stop listening", "delete that" ...: hold everything
        c = k
        if nt[c - 1] in HOLD_LAST: c -= 1                                        # "new" may become "new line" / "new paragraph"
        if c <= n: return
        self._type_words(ph, hyp[n:c])

    def _type_words(self, ph, words):
        out = postprocess(' '.join(words))
        if ph.sess.first and ph.sess.prefix and out: out = ph.sess.prefix + out
        if out: ph.sess.first = False
        txt = '' if not out else (out if out.endswith('\n') else out + ' ')
        if not ph.words: self.last_typed = ''
        if txt:
            t = time.time(); how = self.typer.type(txt); log('stream typed %d word(s) via %s in %.2fs' % (len(words), how, time.time() - t))
        ph.chunks.append((len(words), len(txt), '\n' in txt)); ph.words += words; ph.words_n += [norm(w) for w in words]; self.last_typed += txt

    def stream_finish(self, ph, text):
        """End of a streamed phrase: type what the final pass adds. Typed text is not touched, except a bounded correction (<= 32 chars, never across a newline) inside this phrase."""
        F = text.split(); Fn = [norm(w) for w in F]; T = ph.words_n; nT = len(T)
        k = 0
        while k < min(nT, len(Fn)) and T[k] == Fn[k]: k += 1
        if k == nT:
            rem = F[nT:]; fix = ''
            if nT and len(F) >= nT and ph.chunks and not ph.chunks[-1][2]:
                lw, fw = ph.words[-1], F[nT - 1]
                if fw.startswith(lw) and len(fw) > len(lw) and all(ch in '.,!?;:' for ch in fw[len(lw):]): fix = fw[len(lw):]
            if fix:
                if self.typer.backspace(1): self.last_typed = self.last_typed[:-1]
                else: fix = ''
            out = postprocess(' '.join(rem)) if rem else ''
            txt = fix + ((' ' if fix else '') + (out if out.endswith('\n') else out + ' ') if out else (' ' if fix else ''))
            if txt:
                how = self.typer.type(txt); self.last_typed += txt; log('stream final: typed remainder (%d word(s)%s) via %s' % (len(rem), ', +punctuation' if fix else '', how))
            return
        # the final pass disagrees with words typed from partials: find the chunk the first difference is in
        w = 0; ci = 0
        while ci < len(ph.chunks) and w + ph.chunks[ci][0] <= k: w += ph.chunks[ci][0]; ci += 1
        chars = sum(c[1] for c in ph.chunks[ci:]); nl = any(c[2] for c in ph.chunks[ci:])
        if chars <= 32 and not nl and ci < len(ph.chunks):
            if self.typer.backspace(chars):
                self.last_typed = self.last_typed[:len(self.last_typed) - chars]
                out = postprocess(' '.join(F[w:])); txt = (out if out.endswith('\n') else out + ' ') if out else ''
                if txt: self.typer.type(txt); self.last_typed += txt
                log('stream final: corrected the last %d chars (hypothesis revised)' % chars); return
        rem = F[nT:] if len(F) > nT else []
        if rem:
            out = postprocess(' '.join(rem)); txt = (out if out.endswith('\n') else out + ' ')
            self.typer.type(txt); self.last_typed += txt
        log('stream final: kept typed text, differs from the final pass after word %d (%d chars not corrected)' % (k, chars))

    # --- transcription + typing
    def worker(self):
        while True:
            try: audio, sess, ph = self.q.get(timeout=0.02)
            except queue.Empty:
                pt = self.partial
                if pt is not None:
                    self.partial = None
                    try: self.do_partial(*pt)
                    except Exception as e: log('partial error', repr(e)); pt[0].off = True
                    finally: pt[0].inflight = False
                continue
            try: self.phrase(audio, sess, ph)
            except Exception as e:
                log('phrase error', repr(e)); self.error(str(e) if isinstance(e, NotReady) else 'Could not type the phrase', detail='Click an editable text field and try again. If nothing is typed, open Settings > Setup & uninstall to check typing permissions and packages. Part of the phrase may already have been typed; check it before repeating.')
            finally:
                self.pending -= 1; self.q.task_done()

    def phrase(self, audio, sess, ph=None):
        while self.asr is None and not self.asr_err and not EXTRA.get('no_model'): time.sleep(0.05)
        if self.asr is None: self.error('No speech model yet' if EXTRA.get('no_model') else 'Speech model failed to load', detail='Open Settings > Speech model. Download a model, or select a smaller installed model. If the graphics-card model fails, try Processor.'); return
        t = time.time()
        try:
            with ASR_LOCK: text = self.asr.transcribe(audio).strip()  # A model switch holds the same lock.
        except Exception as e:
            log('transcription failed', repr(e))
            self.error('Speech recognition failed', detail='Try again. If this repeats, open Settings > Speech model and select a smaller model or Processor. Your microphone recording is not saved.')
            return
        tt = time.time() - t
        dur = len(audio) / RATE
        devlog.event('recognition', 'FINAL', text=text, audio_seconds=dur, processing_seconds=tt, model=self.cur_id)
        log('transcribed %.1fs audio in %.2fs (%d chars)%s' % (dur, tt, len(text), (': %r' % text) if DRY else ''))   # transcript detail is recorded only by the opt-in developer logger
        if ph is not None and self._stream_mode() == 'auto' and tt > 1.5 and tt > 0.5 * dur and time.time() >= self.stream_block_until:    # the final pass alone was slow: don't probe a model like that with partial passes
            self.stream_block_until = time.time() + 60; log('model too slow to stream (final pass %.1fs for %.1fs of audio): whole-phrase mode for the next 60 s' % (tt, dur))
        if ph is not None and ph.passes: log('stream phrase: %d partial pass(es), avg %.2fs, %d word(s) typed before the end' % (ph.passes, ph.t_sum / ph.passes, len(ph.words)))
        if ph is not None and ph.words: self.stream_finish(ph, text); return
        n = norm(text)
        if not n or (dur < 2.0 and n in HALLUC): return
        if n in CMD_PAUSE: log('voice command: pause'); threading.Thread(target=lambda: self.handle({'cmd': 'pause'}), daemon=True).start(); return
        if n in CMD_CLOSE: log('voice command: close'); threading.Thread(target=lambda: self.handle({'cmd': 'close'}), daemon=True).start(); return
        if n in CMD_DELETE:
            if self.typer.backspace(len(self.last_typed)): self.last_typed = ''
            return
        out = postprocess(text)
        devlog.event('recognition', 'FORMATTED', original=text, formatted=out)
        if sess.first and sess.prefix: out = sess.prefix + out
        sess.first = False
        out += ' ' if out and not out.endswith('\n') and customization.settings(CFG)['trailing_space'] else ''
        t = time.time(); how = self.typer.type(out); self.last_typed = out
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
            devlog.event('daemon', 'IPC ERROR', traceback=__import__('traceback').format_exc())
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
