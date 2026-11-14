#!/usr/bin/python3
"""Types text into the focused window on GNOME Wayland via the RemoteDesktop portal
(keysym injection: handles unicode/punctuation, no /dev/uinput, no root). Used only when /dev/uinput is not writable.
stdin: text to type (read after the session is ready). stdout: READY / FAIL <why>.
First ever run shows a one-time GNOME "Remote control" consent dialog; the restore token
is saved in the config dir (portal_token) so later runs are silent. uninstall.sh removes that permission again."""
import gi, os, sys, time, random, re, subprocess
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'gnome-voice-typing', 'portal_token')
DEST, OBJ = 'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop'
IFACE = 'org.freedesktop.portal.RemoteDesktop'
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
sender = bus.get_unique_name()[1:].replace('.', '_')
loop = GLib.MainLoop()

def request(method, sig, args, options, timeout=150):
    token = 'gvt%d' % random.randrange(10**9)
    options['handle_token'] = GLib.Variant('s', token)
    path = '/org/freedesktop/portal/desktop/request/%s/%s' % (sender, token)
    res = {}
    def on_resp(conn, snd, p, ifc, sig_, params):
        res['r'] = params.unpack(); loop.quit()
    sub = bus.signal_subscribe(DEST, 'org.freedesktop.portal.Request', 'Response', path, None, 0, on_resp)
    bus.call_sync(DEST, OBJ, IFACE, method, GLib.Variant(sig, args + (options,)), None, 0, -1, None)
    tid = GLib.timeout_add_seconds(timeout, loop.quit)
    loop.run()
    GLib.source_remove(tid)
    bus.signal_unsubscribe(sub)
    if 'r' not in res: raise RuntimeError('portal timeout (consent dialog not answered?)')
    code, results = res['r']
    if code != 0: raise RuntimeError('portal request %s denied/cancelled (code %d)' % (method, code))
    return results

def start_session():
    r = request('CreateSession', '(a{sv})', (), {'session_handle_token': GLib.Variant('s', 'gvt%d' % random.randrange(10**9))})
    session = r['session_handle']
    opts = {'types': GLib.Variant('u', 1), 'persist_mode': GLib.Variant('u', 2)}
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if t: opts['restore_token'] = GLib.Variant('s', t)
    request('SelectDevices', '(oa{sv})', (session,), opts)
    r = request('Start', '(osa{sv})', (session, ''), {})
    if r.get('restore_token'):
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f: f.write(r['restore_token'])
        os.chmod(TOKEN_FILE, 0o600)
    return session

def keysym_for(ch):
    o = ord(ch)
    if ch == '\n': return 0xff0d
    if ch == '\t': return 0xff09
    if o < 0x20: return None
    if o < 0x7f or 0xa0 <= o <= 0xff: return o
    return 0x01000000 | o

def key(session, ks, state):
    bus.call_sync(DEST, OBJ, IFACE, 'NotifyKeyboardKeysym',
                  GLib.Variant('(oa{sv}iu)', (session, {}, ks, state)), None, 0, -1, None)

def type_text(session, text):
    for a, b in (('\u2014', ' - '), ('\u2013', '-'), ('\u2018', "'"), ('\u2019', "'"), ('\u201c', '"'), ('\u201d', '"'), ('\u2026', '...'), ('\u00a0', ' ')):
        text = text.replace(a, b)
    old_clip = None
    for seg in re.findall(r'[\x00-\x7f]+|[^\x00-\x7f]+', text):
        if seg[0] < '\x80':
            for ch in seg:
                ks = keysym_for(ch)
                if ks is None: continue
                key(session, ks, 1); key(session, ks, 0)
                time.sleep(0.004)
        else:
            # mutter cannot synthesize keysyms outside the active layout -> paste via clipboard (Shift+Insert)
            if old_clip is None:
                r = subprocess.run(['wl-paste', '--no-newline', '--type', 'text/plain'], capture_output=True, timeout=3)
                old_clip = r.stdout if r.returncode == 0 else b''
            for extra in ([], ['--primary']):
                subprocess.run(['wl-copy'] + extra + ['--', seg], timeout=5)
            time.sleep(0.15)
            for code, st in ((42, 1), (110, 1), (110, 0), (42, 0)):
                bus.call_sync(DEST, OBJ, IFACE, 'NotifyKeyboardKeycode', GLib.Variant('(oa{sv}iu)', (session, {}, code, st)), None, 0, -1, None)
            time.sleep(0.3)
    if old_clip is not None:
        try:
            subprocess.run(['wl-copy', '--', old_clip.decode('utf-8', 'replace')] if old_clip else ['wl-copy', '--clear'], timeout=5)
        except Exception: pass

def main():
    try:
        session = start_session()
    except Exception as e:
        print('FAIL %s' % e, flush=True); sys.exit(1)
    print('READY', flush=True)
    if '--setup' in sys.argv:
        return
    if '--serve' in sys.argv:       # persistent helper: one JSON line per phrase, answers OK
        import json
        for line in sys.stdin:
            try: type_text(session, json.loads(line)['text'])
            except Exception as e: print('ERR', e, file=sys.stderr, flush=True)
            print('OK', flush=True)
    else:
        type_text(session, sys.stdin.buffer.read().decode('utf-8', 'replace'))
    time.sleep(0.15)
    try:
        bus.call_sync(DEST, session, 'org.freedesktop.portal.Session', 'Close', None, None, 0, -1, None)
    except Exception: pass
main()
