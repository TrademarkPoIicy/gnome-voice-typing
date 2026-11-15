#!/usr/bin/python3
"""Thin hotkey client: sends a command to the warm daemon (starting it if needed)."""
import os, sys, socket, json, subprocess, time, datetime
N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.environ.get('XDG_RUNTIME_DIR') or ('/tmp/gnome-voice-typing-%d' % os.getuid()); SOCK = RT + '/gnome-voice-typing.sock'
cmd = sys.argv[1] if len(sys.argv) > 1 else 'toggle'
HELP = """usage: voice-typing [toggle|start|pause|resume|close|status|quit]
       voice-typing --models | --model <id> | --apply '{"device":"cpu"}' | --apply '{"sounds":false}' | --check
  toggle   start listening, or pause/resume (this is what the keyboard shortcut runs)
  close    stop dictation and hide the bar         status   print the daemon state (JSON)
  quit     stop the background daemon              --check  quick environment check
  calibrate  measure the room noise for 2 s (not while listening) and save it     meter  keep the live mic level running for ~6 s"""
if cmd in ('--check', 'check'):
    print('uinput writable:', os.access('/dev/uinput', os.W_OK), '| daemon socket:', os.path.exists(SOCK)); sys.exit(0)
if cmd in ('-h', '--help', 'help'):
    print(HELP); sys.exit(0)
if cmd == '--models': cmd = 'models'
if cmd == '--model':
    if len(sys.argv) < 3: print('usage: voice-typing --model <id>   (list: voice-typing --models)'); sys.exit(2)
    msg = {'cmd': 'apply', 'model': sys.argv[2]}; cmd = 'apply'
elif cmd == '--apply':
    msg = dict(json.loads(sys.argv[2]), cmd='apply'); cmd = 'apply'
if cmd not in ('apply', 'models', 'toggle', 'start', 'pause', 'resume', 'close', 'status', 'quit', 'testwav', 'calibrate', 'meter'):
    print(HELP); sys.exit(2)
msg = locals().get('msg') or {'cmd': cmd}
if cmd == 'testwav':
    msg.update(path=sys.argv[2], prefix=sys.argv[3] if len(sys.argv) > 3 else '', enter=len(sys.argv) > 4 and sys.argv[4] == 'enter')
def send():
    s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect(SOCK)
    s.sendall((json.dumps(msg) + '\n').encode()); r = s.makefile().readline(); s.close(); return r
try:
    print(send().strip())
except Exception:
    if cmd == 'status': print('{"state": "stopped"}'); sys.exit(0)
    if cmd in ('quit', 'close', 'pause', 'meter'): sys.exit(0)
    subprocess.Popen([N + '/bin/voice-typingd', '--show-start'], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        time.sleep(0.1)
        try: print(send().strip()); break
        except Exception: pass
