#!/usr/bin/python3
"""Real GSettings integration using a private in-memory backend."""
import argparse
import os
os.environ['GSETTINGS_BACKEND'] = 'memory'
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/lib'))
import gvt_gnome as g
import gvt_shortcuts as s
from gi.repository import Gio


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name + '/state.json'
        self.mk = Gio.Settings.new(g.MK)
        self.other = g.KB_BASE + 'other-app/'
        self.mk.set_strv('custom-keybindings', [self.other])
        other = Gio.Settings.new_with_path(g.CUSTOM, self.other)
        other.set_string('name', 'Other app')
        other.set_string('binding', '<Control><Alt>z')
        for slot in s.SLOTS.values():
            cs = Gio.Settings.new_with_path(g.CUSTOM, slot)
            for key in ('name', 'binding', 'command'): cs.reset(key)

    def test_save_edit_disable_and_uninstall(self):
        bindings = {'toggle': '<Control><Alt><Super><Shift>F8', 'close': '<Control><Alt><Super><Shift>F9'}
        s.save(bindings, self.state, '/tmp/voice typing/bin/voice-typing')
        self.assertEqual(s.current(), s.validate(bindings))
        bindings['toggle'] = ''; s.save(bindings, self.state, '/tmp/voice typing/bin/voice-typing')
        self.assertNotIn(s.SLOTS['toggle'], g.custom_slots())
        self.assertIn(self.other, g.custom_slots())
        g.cmd_restore(argparse.Namespace(state=self.state, dry_run=False))
        self.assertEqual(g.custom_slots(), [self.other])
        self.assertEqual(Gio.Settings.new_with_path(g.CUSTOM, self.other).get_string('binding'), '<Control><Alt>z')

    def test_conflicts_leave_settings_unchanged(self):
        with self.assertRaisesRegex(ValueError, 'Other app'):
            s.save({'toggle': '<Control><Alt>z', 'close': ''}, self.state, '/tmp/voice-typing')
        self.assertEqual(g.custom_slots(), [self.other])
        self.assertFalse(Path(self.state).exists())

    def test_duplicate_and_plain_keys_rejected(self):
        for bindings in ({'toggle': 'h', 'close': ''}, {'toggle': '<Ctrl><Alt>h', 'close': '<Control><Alt>h'}):
            with self.assertRaises(ValueError): s.validate(bindings)


if __name__ == '__main__': unittest.main()
