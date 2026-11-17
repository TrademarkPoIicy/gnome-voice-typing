"""Opt-in, local diagnostic sessions shared by the daemon and its helpers."""
import contextlib
import datetime
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import subprocess
import threading
import traceback
import uuid

APP = 'gnome-voice-typing'
ROOT = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / APP / 'logs'
CONFIG = Path(os.environ.get('GVT_CONFIG') or (Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / APP / 'config.json'))
INSTALL = Path(__file__).resolve().parent.parent
_lock = threading.RLock()
last_error = ''


def enabled():
    try: return json.loads(CONFIG.read_text()).get('developer_logging') is True
    except (OSError, ValueError): return False


def _prepare():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)


def _open(path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, 'a', encoding='utf-8')


@contextlib.contextmanager
def _guard():
    with _lock:
        _prepare()
        with _open(ROOT / '.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def latest():
    try:
        name = (ROOT / 'latest').read_text().strip()
        if Path(name).name != name or not name.startswith('session-') or not name.endswith('.txt'): return None
        path = ROOT / name
        return path if path.is_file() and not path.is_symlink() else None
    except OSError: return None


def _line(path, source, event, fields):
    stamp = datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')
    details = []
    blocks = []
    for key, value in fields.items():
        if isinstance(value, str) and '\n' in value:
            # Indentation keeps multiline speech/output readable without making
            # it look like a new timestamped diagnostic event.
            blocks.append('    ' + key + ':\n' + '\n'.join('        ' + line for line in value.splitlines()))
        else:
            details.append('%s=%s' % (key, json.dumps(value, ensure_ascii=False, default=str)))
    with _open(path) as out:
        out.write('%s [%s pid=%d] %s%s\n' % (stamp, source, os.getpid(), event, (' | ' + ' | '.join(details)) if details else ''))
        if blocks: out.write('\n'.join(blocks) + '\n')


def _new(reason):
    name = 'session-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.txt'
    path = ROOT / name
    with _open(path) as out:
        out.write('GNOME Voice Typing — developer session log\nContains recognized speech and settings. Review before sharing. No raw audio is recorded.\n\n')
    temp = ROOT / ('.latest-' + uuid.uuid4().hex)
    with _open(temp) as out: out.write(name)
    os.replace(temp, ROOT / 'latest')
    try: version = (INSTALL / 'VERSION').read_text().strip()
    except OSError: version = 'unknown'
    try: distro = platform.freedesktop_os_release().get('PRETTY_NAME')
    except OSError: distro = None
    try:
        gnome = subprocess.run(['gnome-shell', '--version'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired): gnome = 'unavailable'
    gpu = []
    for device in Path('/sys/class/drm').glob('card[0-9]*/device'):
        try: gpu.append({key: (device / key).read_text().strip() for key in ('vendor', 'device')})
        except OSError: pass
    packages = {}
    for name in ('numpy', 'sherpa-onnx', 'onnxruntime', 'faster-whisper'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = 'not installed'
    _line(path, 'session', 'START', dict(reason=reason, version=version, distro=distro,
          architecture=platform.machine(), processor=platform.processor(), kernel=platform.release(), gnome=gnome, gpu_pci=gpu,
          python=sys.version, desktop=os.environ.get('XDG_CURRENT_DESKTOP'), display=os.environ.get('XDG_SESSION_TYPE'), packages=packages))
    try: _line(path, 'session', 'SETTINGS', json.loads(CONFIG.read_text()))
    except (OSError, ValueError): pass
    return path


def start(reason):
    global last_error
    try:
        if not enabled(): return
        with _guard():
            if enabled(): _new(reason); last_error = ''
    except Exception as error: last_error = str(error)  # Diagnostics must never break dictation.


def event(source, name, **fields):
    global last_error
    try:
        if not enabled(): return
        with _guard():
            if not enabled(): return
            path = latest() or _new('first diagnostic event')
            _line(path, source, name, fields)
            last_error = ''
    except Exception as error: last_error = str(error)


def capture_lines(stream, source, name):
    def drain():
        try:
            for line in iter(stream.readline, b''):
                event(source, name, message=line.decode('utf-8', errors='replace').rstrip())
        except (OSError, ValueError): pass
        finally: stream.close()
    threading.Thread(target=drain, daemon=True).start()


def install_exception_hooks(source):
    previous = sys.excepthook
    def hook(kind, value, tb):
        event(source, 'UNHANDLED EXCEPTION', traceback=''.join(traceback.format_exception(kind, value, tb)))
        previous(kind, value, tb)
    sys.excepthook = hook
    previous_thread = threading.excepthook
    def thread_hook(args):
        event(source, 'THREAD EXCEPTION', thread=args.thread.name,
              traceback=''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        previous_thread(args)
    threading.excepthook = thread_hook


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'latest'
    if action == 'folder':
        _prepare(); print(ROOT)
    elif action == 'event':
        event('overlay', sys.argv[2], detail=sys.argv[3] if len(sys.argv) > 3 else '')
    elif action in ('latest', 'text'):
        with _guard():
            path = latest()
            if path is None: raise RuntimeError('No session log yet. Enable logging, save, then reproduce the issue.')
            output = path.read_text() if action == 'text' else str(path)
        print(output, end='' if action == 'text' else '\n')
    else: raise RuntimeError('Unknown log action.')


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(str(error), file=sys.stderr); sys.exit(1)
