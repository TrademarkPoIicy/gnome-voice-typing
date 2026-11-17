"""User-editable GNOME shortcuts, preserving unrelated shortcuts and uninstall state."""
import shlex
from gi.repository import Gio
import gvt_gnome as gnome

SLOTS = {'toggle': gnome.SLOT, 'close': gnome.KB_BASE + 'gnome-voice-typing-close/'}
LABELS = {'toggle': 'Start / pause dictation', 'close': 'Close dictation'}


def current():
    registered = gnome.custom_slots()
    return {key: Gio.Settings.new_with_path(gnome.CUSTOM, path).get_string('binding') if path in registered else ''
            for key, path in SLOTS.items()}


def validate(bindings):
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gdk
    normalized = {}
    for action, value in bindings.items():
        if action not in SLOTS: raise ValueError('Unknown shortcut action.')
        if not value:
            normalized[action] = ''
            continue
        ok, key, mods = Gtk.accelerator_parse(value)
        if not ok or not Gtk.accelerator_valid(key, mods) or not (mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)):
            raise ValueError('Use Control, Alt, or Super with a key for ' + LABELS[action] + '.')
        normalized[action] = Gtk.accelerator_name(key, mods)
    used = [gnome.norm(x) for x in normalized.values() if x]
    if len(used) != len(set(used)): raise ValueError('Choose a different shortcut for each action.')
    for action, value in normalized.items():
        conflicts = [c for c in gnome.find_conflicts(value, own=SLOTS[action]) if c.get('path') not in SLOTS.values()]
        if conflicts: raise ValueError('Shortcut already used by ' + conflicts[0]['desc'] + '. Choose another combination.')
    return normalized


def save(bindings, state_path, cli):
    bindings = validate(bindings)
    if set(bindings) != set(SLOTS): raise ValueError('Both shortcut actions are required.')
    mk = Gio.Settings.new(gnome.MK)
    registered = list(mk.get_strv('custom-keybindings'))
    state = gnome.load_state(state_path)
    data = state.setdefault('gnome', {})
    data.setdefault('custom_keybindings_old', gnome.user_value(mk, 'custom-keybindings'))
    data['slot'] = SLOTS['toggle']
    data['extra_slots'] = sorted(set(data.get('extra_slots', [])) | {SLOTS['close']})
    data['binding'] = bindings['toggle']
    # Save cleanup ownership before writing GNOME settings.
    for action, path in SLOTS.items():
        st = Gio.Settings.new_with_path(gnome.CUSTOM, path)
        if not all(st.is_writable(k) for k in ('name', 'command', 'binding')):
            raise ValueError('Your desktop administrator has locked shortcut settings.')
    if not mk.is_writable('custom-keybindings'): raise ValueError('Your desktop administrator has locked shortcut settings.')
    gnome.save_state(state_path, state)
    for action, path in SLOTS.items():
        st = Gio.Settings.new_with_path(gnome.CUSTOM, path)
        st.set_string('name', 'Voice typing — ' + LABELS[action])
        st.set_string('command', shlex.quote(cli) + ' ' + action)
        st.set_string('binding', bindings[action])
        if bindings[action] and path not in registered: registered.append(path)
        if not bindings[action] and path in registered: registered.remove(path)
    mk.set_strv('custom-keybindings', registered)
    Gio.Settings.sync()
