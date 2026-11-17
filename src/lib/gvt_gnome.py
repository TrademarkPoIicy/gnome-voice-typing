#!/usr/bin/env python3
"""GNOME settings integration for gnome-voice-typing (used by install.sh / uninstall.sh).

Every setting it changes is recorded (with its previous user value, or "was default") in the install-state file,
so `restore` can put things back exactly. Uses Gio.Settings, so it honours GSETTINGS_BACKEND / XDG_CONFIG_HOME (tests run it
against a throw-away keyfile backend and never touch the real dconf database).

  gvt_gnome.py conflicts BINDING
  gvt_gnome.py apply   --state FILE --uuid UUID [--binding B] [--command CMD] [--move-to ACCEL] [--no-resolve-conflicts] [--no-a11y] [--dry-run]
       defaults: binding <Super>h, command ~/.local/bin/voice-typing toggle, conflicting shortcuts moved to <Super><Shift>h (and recorded with their REAL old value)
  gvt_gnome.py restore --state FILE [--dry-run]
"""
import argparse, json, os, re, sys
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

KB_BASE = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
SLOT = KB_BASE + 'gnome-voice-typing/'
MK = 'org.gnome.settings-daemon.plugins.media-keys'
CUSTOM = MK + '.custom-keybinding'
SCAN = ['org.gnome.desktop.wm.keybindings', 'org.gnome.shell.keybindings', 'org.gnome.mutter.keybindings',
        'org.gnome.mutter.wayland.keybindings', MK]
ALIASES = {'primary': 'control', 'ctrl': 'control', 'mod4': 'super', 'mod1': 'alt', 'meta': 'alt'}

def scan_schemas():
    """the known keybinding schemas first, then every other installed non-relocatable schema (extensions such as dash-to-dock / tiling tools keep shortcuts in their own schemas)"""
    src = Gio.SettingsSchemaSource.get_default(); out = [x for x in SCAN if have_schema(x)]
    if src is None: return out
    for sid in sorted(src.list_schemas(True)[0]):
        if sid not in out and not sid.startswith('org.gnome.desktop.input-sources'): out.append(sid)
    return out

def eff_text(st, key, vt):
    """the EFFECTIVE value as text (what the user actually has right now, default or not)"""
    return vtext(GLib.Variant.new_strv(st.get_strv(key))) if vt == 'as' else vtext(GLib.Variant.new_string(st.get_string(key)))

def canon_command(cmd):
    """the shortcut must run the installed command: ~/.local/bin/voice-typing with an explicit `toggle` (absolute path, no PATH lookup)"""
    lb = os.path.expanduser('~/.local/bin/voice-typing')
    if not cmd or os.path.basename(cmd.split()[0]) in ('voice-typing', 'dictate'):
        if os.path.exists(lb): cmd = lb + ' ' + (' '.join(cmd.split()[1:]) if cmd and len(cmd.split()) > 1 else 'toggle')
        elif cmd and len(cmd.split()) == 1: cmd += ' toggle'
    return cmd

def norm(acc):
    """'<Super><Alt>h' -> (frozenset(mods), 'h'); None if it is not an accelerator."""
    if not isinstance(acc, str) or not acc: return None
    mods = {ALIASES.get(m.lower(), m.lower()) for m in re.findall(r'<([^>]+)>', acc)}
    key = re.sub(r'<[^>]+>', '', acc).strip().lower()
    return (frozenset(mods), key) if key else None

def have_schema(sid):
    src = Gio.SettingsSchemaSource.get_default()
    return src is not None and src.lookup(sid, True) is not None

def vtext(v): return None if v is None else v.print_(True)

def user_value(st, key):
    v = st.get_user_value(key)
    return vtext(v)

def put(st, key, text):
    """restore a saved value: None -> reset to default."""
    if text is None: st.reset(key)
    else: st.set_value(key, GLib.Variant.parse(None, text))
    Gio.Settings.sync()

def custom_slots():
    mk = Gio.Settings.new(MK)
    return list(mk.get_strv('custom-keybindings'))

def find_conflicts(binding, own=SLOT):
    want = norm(binding); out = []
    if want is None: return out
    for sid in scan_schemas():
        try: st = Gio.Settings.new(sid)
        except Exception: continue
        sch = st.props.settings_schema
        for key in sch.list_keys():
            vt = sch.get_key(key).get_value_type().dup_string()
            if vt == 'as': hits = [a for a in st.get_strv(key) if norm(a) == want]
            elif vt == 's': hits = [a for a in [st.get_string(key)] if norm(a) == want]
            else: continue
            if hits: out.append({'schema': sid, 'key': key, 'type': vt, 'accel': hits[0], 'desc': '%s %s' % (sid, key)})
    for path in custom_slots():
        if path == own: continue
        st = Gio.Settings.new_with_path(CUSTOM, path)
        if norm(st.get_string('binding')) == want:
            out.append({'schema': CUSTOM, 'path': path, 'key': 'binding', 'type': 's', 'accel': st.get_string('binding'),
                        'desc': 'custom shortcut "%s"' % st.get_string('name')})
    return out

def load_state(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return {}

def save_state(p, s):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f: json.dump(s, f, indent=2)
    os.replace(tmp, p)

def settings_for(rec):
    return Gio.Settings.new_with_path(rec['schema'], rec['path']) if rec.get('path') else Gio.Settings.new(rec['schema'])

def cmd_conflicts(a):
    print(json.dumps(find_conflicts(a.binding))); return 0

def cmd_apply(a):
    dry = a.dry_run; S = load_state(a.state); G = S.setdefault('gnome', {})
    def say(m): print(('[dry-run] ' if dry else '') + m)
    if not have_schema('org.gnome.shell'): print('GNOME Shell settings schema not found', file=sys.stderr); return 2
    # 1. shortcut conflicts: MOVE the conflicting shortcut to --move-to (default <Super><Shift>h) instead of just deleting it, and save the REAL old value
    #    (not "was default") plus what we set, so uninstall can put it back exactly
    saved = G.setdefault('conflicts', [])
    if a.binding and not a.no_resolve_conflicts:
        moved_to = a.move_to if a.move_to and not find_conflicts(a.move_to) else None      # only if that accelerator is free
        for c in find_conflicts(a.binding):
            st = settings_for(c)
            if not any(x['schema'] == c['schema'] and x.get('path') == c.get('path') and x['key'] == c['key'] for x in saved):
                saved.append({'schema': c['schema'], 'path': c.get('path'), 'key': c['key'], 'old': eff_text(st, c['key'], c['type']), 'old_user': user_value(st, c['key'])})
            rec = next(x for x in saved if x['schema'] == c['schema'] and x.get('path') == c.get('path') and x['key'] == c['key'])
            if c['type'] == 'as':
                cur = st.get_strv(c['key']); new = []
                for x in cur:
                    if norm(x) != norm(a.binding): new.append(x)
                    elif moved_to and moved_to not in new and moved_to not in cur: new.append(moved_to)
                say('%s: %s -> %s (was %s)' % (c['desc'], c['accel'], moved_to or 'removed', cur))
                if not dry: st.set_strv(c['key'], new)
                rec['new'] = vtext(GLib.Variant.new_strv(new))
            else:
                say('%s: %s -> %s' % (c['desc'], c['accel'], moved_to or 'cleared'))
                if not dry: st.set_string(c['key'], moved_to or '')
                rec['new'] = vtext(GLib.Variant.new_string(moved_to or ''))
            if moved_to: moved_to = None            # the first conflict gets the free accelerator; any further one is cleared, never doubled up
    # 2. our custom shortcut slot (appended to the existing custom shortcuts, never replacing them)
    if a.binding:
        a.command = canon_command(a.command)
        mk = Gio.Settings.new(MK); slots = list(mk.get_strv('custom-keybindings'))
        if 'custom_keybindings_old' not in G: G['custom_keybindings_old'] = user_value(mk, 'custom-keybindings')
        say('custom shortcut "Voice typing" = %s -> %s' % (a.binding, a.command))
        if not dry:
            if SLOT not in slots: mk.set_strv('custom-keybindings', slots + [SLOT])
            cs = Gio.Settings.new_with_path(CUSTOM, SLOT)
            cs.set_string('name', 'Voice typing'); cs.set_string('command', a.command); cs.set_string('binding', a.binding)
        G['binding'] = a.binding; G['slot'] = SLOT
    # 3. accessibility (lets the bar find the focused text box)
    if not a.no_a11y:
        ia = Gio.Settings.new('org.gnome.desktop.interface')
        if 'toolkit_accessibility_old' not in G: G['toolkit_accessibility_old'] = user_value(ia, 'toolkit-accessibility')
        G['toolkit_accessibility_was_true'] = ia.get_boolean('toolkit-accessibility')
        say('toolkit-accessibility: %s -> true' % ia.get_boolean('toolkit-accessibility'))
        if not dry: ia.set_boolean('toolkit-accessibility', True)
    # 4. enable the extension
    sh = Gio.Settings.new('org.gnome.shell'); en = list(sh.get_strv('enabled-extensions'))
    if 'enabled_extensions_old' not in G: G['enabled_extensions_old'] = user_value(sh, 'enabled-extensions')
    G['uuid'] = a.uuid
    say('enabled-extensions: adding %s' % a.uuid)
    if not dry and a.uuid not in en: sh.set_strv('enabled-extensions', en + [a.uuid])
    if sh.get_boolean('disable-user-extensions'): print('WARNING: GNOME "disable-user-extensions" is on; the bar will not load until you turn user extensions back on.')
    if not dry: Gio.Settings.sync(); save_state(a.state, S)
    return 0

def cmd_restore(a):
    dry = a.dry_run; S = load_state(a.state); G = S.get('gnome') or {}
    def say(m): print(('[dry-run] ' if dry else '') + m)
    if not G: print('nothing recorded in %s; no GNOME settings to restore' % a.state); return 0
    for c in G.get('conflicts', []):
        if not have_schema(c['schema']): continue
        st = settings_for(c); vt = st.props.settings_schema.get_key(c['key']).get_value_type().dup_string()
        try: cur = eff_text(st, c['key'], vt)
        except Exception: cur = None
        # the user gave this action a different shortcut after install (e.g. minimize -> <Super><Shift>h): keep theirs.
        # Newer state files record what the installer set ('new'); older ones only the old value, then "empty" means untouched.
        if 'new' in c: changed = cur is not None and cur != c['new'] and cur != c['old']
        else: changed = cur not in (None, '@as []', '[]', "''") and cur != c['old']
        if changed:
            say('keeping %s %s = %s (changed since install)' % (c['schema'], c['key'], cur)); continue
        say('restoring %s%s %s -> %s' % (c['schema'], (' @' + c['path']) if c.get('path') else '', c['key'], 'default' if c['old'] is None else c['old']))
        if not dry: put(settings_for(c), c['key'], c['old'])
    owned_slots = ([G['slot']] if G.get('slot') else []) + G.get('extra_slots', [])
    if owned_slots:
        mk = Gio.Settings.new(MK); slots = [p for p in mk.get_strv('custom-keybindings') if p not in owned_slots]
        say('removing Voice typing custom shortcut slots')
        if not dry:
            for path in owned_slots:
                cs = Gio.Settings.new_with_path(CUSTOM, path)
                for k in ('name', 'command', 'binding'): cs.reset(k)
            if G.get('custom_keybindings_old') is None and slots == list(mk.get_default_value('custom-keybindings').unpack()): mk.reset('custom-keybindings')
            else: mk.set_strv('custom-keybindings', slots)
            Gio.Settings.sync()
    if 'toolkit_accessibility_old' in G:
        say('toolkit-accessibility -> %s' % ('default' if G['toolkit_accessibility_old'] is None else G['toolkit_accessibility_old']))
        if not dry: put(Gio.Settings.new('org.gnome.desktop.interface'), 'toolkit-accessibility', G['toolkit_accessibility_old'])
    if G.get('uuid') and have_schema('org.gnome.shell'):
        sh = Gio.Settings.new('org.gnome.shell'); en = [x for x in sh.get_strv('enabled-extensions') if x != G['uuid']]
        say('enabled-extensions: removing %s' % G['uuid'])
        if not dry:
            if G.get('enabled_extensions_old') is None and en == list(sh.get_default_value('enabled-extensions').unpack()): sh.reset('enabled-extensions')
            else: sh.set_strv('enabled-extensions', en)
            Gio.Settings.sync()
    if not dry:
        S.pop('gnome', None); save_state(a.state, S)
    return 0

def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest='cmd', required=True)
    c = sp.add_parser('conflicts'); c.add_argument('binding'); c.set_defaults(f=cmd_conflicts)
    a = sp.add_parser('apply'); a.set_defaults(f=cmd_apply)
    for x in ('--state', '--uuid'): a.add_argument(x, required=True)
    a.add_argument('--command', default='', help='default: ~/.local/bin/voice-typing toggle')
    a.add_argument('--binding', default='<Super>h', help='accelerator; pass an empty string to create no shortcut')
    a.add_argument('--resolve-conflicts', action='store_true'); a.add_argument('--no-resolve-conflicts', action='store_true'); a.add_argument('--move-to', default='<Super><Shift>h'); a.add_argument('--no-a11y', action='store_true'); a.add_argument('--dry-run', action='store_true')
    r = sp.add_parser('restore'); r.add_argument('--state', required=True); r.add_argument('--dry-run', action='store_true'); r.set_defaults(f=cmd_restore)
    args = p.parse_args(); return args.f(args)

if __name__ == '__main__':
    sys.exit(main())
