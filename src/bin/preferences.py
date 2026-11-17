#!/usr/bin/python3
"""Native personalization editor; runs with system GTK, outside the speech venv."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk
import customization

N = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(N / 'lib'))
import gvt_shortcuts

CONFIG = os.environ.get('GVT_CONFIG') or str(Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gnome-voice-typing/config.json')
RT = os.environ.get('XDG_RUNTIME_DIR') or '/tmp/gnome-voice-typing-%d' % os.getuid()
SOCK = RT + '/gnome-voice-typing.sock'


def request(message):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(SOCK)
        sock.sendall((json.dumps(message) + '\n').encode())
        with sock.makefile() as stream:
            reply = json.loads(stream.readline())
    if reply.get('error'): raise ValueError(reply['error'])
    return reply


def save_preferences(values):
    values = customization.validate(values)
    # Never fall back to an offline write after a timeout: the service may be saving.
    try:
        reply = request({'cmd': 'preferences'})
    except (FileNotFoundError, ConnectionRefusedError):
        customization.update_config(CONFIG, values)
        return 'Saved. Changes will apply the next time voice typing starts.'
    if 'settings' not in reply:
        raise ValueError('The background service needs to reload the update. Close dictation, log out and back in, then save again.')
    request(dict(values, cmd='apply'))
    confirmed = request({'cmd': 'preferences'})
    if any(confirmed.get('settings', {}).get(k) != v for k, v in values.items()):
        raise ValueError('The service could not confirm these settings. Reopen Personalization to check before retrying.')
    return 'Saved. Appearance updates now; formatting applies to your next phrase.'


def label(text, dim=False):
    w = Gtk.Label(label=text, xalign=0, wrap=True)
    if dim: w.add_css_class('dim-label')
    return w


class Preferences(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.gnome_voice_typing.Personalization')
        self.connect('activate', self.activate)
        self.window = None
        self.controls = {}
        self.rules = []
        self.recording = None

    def activate(self, app):
        if self.window:
            self.window.present()
            return
        read_error = ''
        try:
            with open(CONFIG) as f: config = json.load(f)
            if not isinstance(config, dict): raise ValueError('Settings must be a JSON object.')
        except FileNotFoundError: config = {}
        except (OSError, ValueError) as e:
            config = {}; read_error = 'Could not read your settings: ' + str(e)
        self.values = customization.settings(config)
        self.saved_values = copy.deepcopy(self.values)
        self.discarding = False
        self.saving = False
        self.window = Gtk.ApplicationWindow(application=self, title='Voice typing — Personalization', default_width=640, default_height=620)
        self.window.connect('close-request', self.close_window)
        header = Gtk.HeaderBar()
        self.window.set_titlebar(header)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16, margin_start=20, margin_end=20)
        self.window.set_child(outer)
        outer.append(label('Make voice typing yours'))
        outer.append(label('Dictation pauses while you edit. Resume it with your shortcut when you are ready.', True))
        self.notebook = Gtk.Notebook(vexpand=True)
        outer.append(self.notebook)
        self.appearance(self.page('Appearance'))
        self.formatting(self.page('Formatting'))
        self.vocabulary(self.page('Vocabulary'))
        self.shortcuts(self.page('Shortcuts'))
        self.status = label(read_error, True)
        self.status.set_selectable(True)
        outer.append(self.status)
        self.save_button = Gtk.Button(label='Save personalization', halign=Gtk.Align.END)
        self.save_button.add_css_class('suggested-action')
        self.save_button.connect('clicked', self.save)
        self.save_button.set_sensitive(not read_error)
        outer.append(self.save_button)
        self.notebook.connect('switch-page', lambda n, p, i: self.save_button.set_visible(i != 3))
        self.window.present()
        # Pause only an already running service; opening preferences never starts recording.
        def pause():
            try: request({'cmd': 'pause'})
            except (OSError, ValueError): pass
        threading.Thread(target=pause, daemon=True).start()

    def close_window(self, window):
        if self.saving:
            self.status.set_text('Please wait for the save to finish.')
            return True
        try: dirty = self.collect() != self.saved_values or self.bindings != self.saved_bindings
        except ValueError: dirty = True
        if dirty and not self.discarding:
            dialog = Gtk.MessageDialog(transient_for=window, modal=True, text='Discard unsaved changes?',
                                       secondary_text='Your saved preferences and shortcuts will stay unchanged.')
            dialog.add_button('Keep editing', Gtk.ResponseType.CANCEL)
            dialog.add_button('Discard', Gtk.ResponseType.ACCEPT)
            def response(d, answer):
                d.destroy()
                if answer == Gtk.ResponseType.ACCEPT:
                    self.discarding = True
                    window.close()
            dialog.connect('response', response)
            dialog.present()
            return True
        self.window = None
        self.controls = {}
        self.rules = []
        self.recording = None
        return False

    def page(self, title):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=20, margin_bottom=20, margin_start=16, margin_end=16)
        scroll.set_child(box)
        self.notebook.append_page(scroll, Gtk.Label(label=title))
        return box

    def row(self, box, title, control, help_text=''):
        row = Gtk.Box(spacing=18)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        text.append(label(title))
        if help_text: text.append(label(help_text, True))
        row.append(text)
        control.set_valign(Gtk.Align.CENTER)
        row.append(control)
        box.append(row)

    def choice(self, box, key, title, options, help_text=''):
        ids, names = zip(*options)
        control = Gtk.DropDown.new_from_strings(names)
        control.set_selected(ids.index(self.values[key]))
        self.controls[key] = lambda: ids[control.get_selected()]
        self.row(box, title, control, help_text)
        return control

    def switch(self, box, key, title, help_text=''):
        control = Gtk.Switch(active=self.values[key])
        self.controls[key] = control.get_active
        self.row(box, title, control, help_text)
        return control

    def appearance(self, box):
        self.choice(box, 'bar_theme', 'Bar theme', [('dark', 'Dark'), ('light', 'Light'), ('contrast', 'High contrast')])
        self.choice(box, 'bar_size', 'Bar size', [('compact', 'Compact'), ('comfortable', 'Comfortable'), ('large', 'Large')], 'Larger sizes increase text and button sizes.')
        opacity = Gtk.SpinButton.new_with_range(70, 100, 1)
        opacity.set_value(self.values['bar_opacity'])
        self.controls['bar_opacity'] = opacity.get_value_as_int
        self.row(box, 'Background opacity (%)', opacity, 'Text and buttons stay fully visible. High contrast uses an opaque background.')
        self.switch(box, 'show_model', 'Show model name', 'Setup and loading messages remain visible.')
        box.append(label('Bar position: use “Move bar to the text field I click” in Settings > Typing & sounds. Turn it off to keep the bar where you place it during dictation.', True))

    def formatting(self, box):
        self.choice(box, 'punctuation', 'Automatic punctuation', [('model', 'Keep model punctuation'), ('none', 'Remove punctuation')], 'Remove commas, periods, question marks, exclamation marks, colons and semicolons.')
        self.choice(box, 'capitalization', 'Capitalization', [('model', 'Keep model capitalization'), ('sentence', 'Capitalize sentence starts'), ('lower', 'Lowercase')])
        self.switch(box, 'spoken_punctuation', 'Spoken punctuation', 'Say “comma”, “period”, “question mark”, “exclamation mark”, “colon” or “semicolon”. Spoken marks are kept even when automatic punctuation is off.')
        self.switch(box, 'trailing_space', 'Add a space after each phrase')
        box.append(label('“New line” and “new paragraph” always work. Custom formatting waits until the end of each phrase before typing.', True))
        box.append(Gtk.Separator())
        box.append(label('Try your formatting and vocabulary'))
        entry = Gtk.Entry(placeholder_text='Type a sample transcription…')
        box.append(entry)
        result = label('')
        result.set_selectable(True)
        box.append(result)
        def preview(*args):
            try:
                result.set_text(customization.final_text(entry.get_text(), self.collect()).replace('\n', ' ↵\n'))
            except ValueError as e: result.set_text(str(e))
        entry.connect('changed', preview)
        test = Gtk.Button(label='Preview')
        test.connect('clicked', preview)
        box.append(test)

    def vocabulary(self, box):
        box.append(label('Teach it your preferred words'))
        box.append(label('Replace a misheard name or phrase after transcription. Matches ignore case and apply to whole words. Your preferred spelling and capitalization are kept.', True))
        box.append(label('Example: “open ai” → “OpenAI”. These rules change the text; they do not retrain the speech model.', True))
        self.rule_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.rule_box)
        for rule in self.values['vocabulary']: self.add_rule(rule['heard'], rule['write'])
        add = Gtk.Button(label='Add word or phrase', halign=Gtk.Align.START)
        add.connect('clicked', lambda b: self.add_rule())
        box.append(add)
        box.append(label('Up to 200 rules. Longer matches take priority. Rules run once, after formatting, and wait for a complete phrase.', True))

    def add_rule(self, heard='', write=''):
        if len(self.rules) >= 200:
            self.status.set_text('You can save up to 200 vocabulary rules.')
            return
        row = Gtk.Box(spacing=8)
        a = Gtk.Entry(text=heard, placeholder_text='Heard phrase', hexpand=True, max_length=200)
        b = Gtk.Entry(text=write, placeholder_text='Write instead', hexpand=True, max_length=200)
        remove = Gtk.Button(label='Remove')
        record = (row, a, b)
        remove.connect('clicked', lambda btn: (self.rule_box.remove(row), self.rules.remove(record)))
        for widget in (a, Gtk.Label(label='→'), b, remove): row.append(widget)
        self.rules.append(record)
        self.rule_box.append(row)

    def shortcuts(self, box):
        box.append(label('Click a shortcut, then press a combination with Control, Alt or Super. Escape cancels. Conflicts are checked before saving.', True))
        self.bindings = gvt_shortcuts.current()
        self.saved_bindings = dict(self.bindings)
        self.binding_buttons = {}
        for action, title in gvt_shortcuts.LABELS.items():
            row = Gtk.Box(spacing=8)
            button = Gtk.Button(label=self.binding_label(self.bindings[action]))
            button.connect('clicked', lambda b, a=action: self.record(a))
            self.binding_buttons[action] = button
            clear = Gtk.Button(label='Disable')
            clear.connect('clicked', lambda b, a=action: self.set_binding(a, ''))
            row.append(button); row.append(clear)
            self.row(box, title, row)
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect('key-pressed', self.key_pressed)
        self.window.add_controller(controller)
        save = Gtk.Button(label='Save shortcuts', halign=Gtk.Align.START)
        save.connect('clicked', self.save_shortcuts)
        box.append(save)
        box.append(label('Shortcut changes take effect immediately. Other applications’ shortcuts are never reassigned. Some existing desktop shortcuts may be intercepted before this window receives them.', True))

    @staticmethod
    def binding_label(binding):
        if not binding: return 'Disabled — click to set'
        ok, key, mods = Gtk.accelerator_parse(binding)
        return Gtk.accelerator_get_label(key, mods) if ok else binding

    def record(self, action):
        if self.recording: self.binding_buttons[self.recording].set_label(self.binding_label(self.bindings[self.recording]))
        self.recording = action
        self.binding_buttons[action].set_label('Press shortcut…')

    def set_binding(self, action, value):
        self.bindings[action] = value
        self.binding_buttons[action].set_label(self.binding_label(value))
        self.recording = None

    def key_pressed(self, controller, key, code, state):
        if not self.recording: return False
        action = self.recording
        if key == Gdk.KEY_Escape:
            self.set_binding(action, self.bindings[action]); return True
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if not Gtk.accelerator_valid(key, mods) or not mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK): return True
        self.set_binding(action, Gtk.accelerator_name(Gdk.keyval_to_lower(key), mods))
        return True

    def collect(self):
        values = {key: get() for key, get in self.controls.items()}
        values['vocabulary'] = [{'heard': a.get_text(), 'write': b.get_text()} for row, a, b in self.rules if a.get_text() or b.get_text()]
        return customization.validate(values)

    def save(self, button):
        try: values = self.collect()
        except ValueError as e: self.status.set_text(str(e)); return
        self.saving = True
        button.set_sensitive(False)
        self.notebook.set_sensitive(False)
        self.status.set_text('Saving…')
        def worker():
            success = False
            try:
                message = save_preferences(values)
                success = True
            except (OSError, ValueError) as e: message = 'Could not save personalization. ' + str(e)
            def finish():
                self.saving = False
                if success: self.saved_values = copy.deepcopy(values)
                if self.window:
                    self.status.set_text(message)
                    button.set_sensitive(True)
                    self.notebook.set_sensitive(True)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def save_shortcuts(self, button):
        try:
            gvt_shortcuts.save(self.bindings, str(N / 'install-state.json'), str(N / 'bin/voice-typing'))
            self.saved_bindings = dict(self.bindings)
            self.status.set_text('Shortcuts saved. They are ready to use.')
        except (ValueError, OSError, GLib.Error) as e: self.status.set_text('Shortcuts were not saved: ' + str(e))


if __name__ == '__main__':
    app = Preferences()
    sys.exit(app.run(sys.argv))
