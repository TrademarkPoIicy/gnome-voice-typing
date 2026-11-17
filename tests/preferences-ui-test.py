#!/usr/bin/python3
"""Run with a test display (GDK_BACKEND=broadway supported); never touches real settings."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/bin'))
import preferences as p
from gi.repository import Gtk

with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'CONFIG', tmp + '/config.json'), patch.object(p, 'SOCK', tmp + '/absent.sock'):
    Gtk.init()
    app = p.Preferences()
    app.register(None)
    app.activate(app)
    assert app.notebook.get_n_pages() == 4
    assert app.collect() == p.customization.DEFAULTS
    app.add_rule('open ai', 'OpenAI')
    values = app.collect()
    assert values['vocabulary'] == [{'heard': 'open ai', 'write': 'OpenAI'}]
    p.save_preferences(values)
    assert p.customization.settings(__import__('json').loads(Path(p.CONFIG).read_text())) == values
    app.record('toggle')
    assert app.key_pressed(None, p.Gdk.KEY_k, 0, p.Gdk.ModifierType.CONTROL_MASK | p.Gdk.ModifierType.ALT_MASK)
    assert app.bindings['toggle'] == '<Control><Alt>k'
    for index in range(4): app.notebook.set_current_page(index)
    app.window.destroy()
    print('PASS native window, four pages, vocabulary editing, shortcut capture, offline save and reload')
