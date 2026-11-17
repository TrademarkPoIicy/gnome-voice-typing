#!/usr/bin/python3
"""Exercise real IPC and offline saving, with isolated fake services."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/bin'))
import preferences as p


class PreferencesIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = self.tmp.name + '/daemon.sock'
        self.config = self.tmp.name + '/config.json'
        for key, value in [('CONFIG', self.config), ('SOCK', self.socket_path)]:
            ctx = patch.object(p, key, value); ctx.start(); self.addCleanup(ctx.stop)

    def service(self, replies):
        server = socket.socket(socket.AF_UNIX)
        server.bind(self.socket_path); server.listen(3); server.settimeout(3)
        self.addCleanup(server.close)
        def run():
            for reply in replies:
                conn, _ = server.accept()
                with conn, conn.makefile() as stream:
                    message = json.loads(stream.readline())
                    response = reply(message) if callable(reply) else reply
                    conn.sendall((json.dumps(response) + '\n').encode())
        thread = threading.Thread(target=run, daemon=True); thread.start()
        return thread

    def test_offline_persists_and_preserves_model(self):
        Path(self.config).write_text('{"model":"existing"}')
        p.save_preferences({'bar_theme': 'light'})
        self.assertEqual(json.loads(Path(self.config).read_text()), {'model': 'existing', 'bar_theme': 'light'})

    def test_live_confirms_saved_values(self):
        seen = []
        def apply(message): seen.append(message); return {'ok': True}
        thread = self.service([{'settings': p.customization.DEFAULTS}, apply, {'settings': {'bar_theme': 'light'}}])
        p.save_preferences({'bar_theme': 'light'}); thread.join(1)
        self.assertEqual(seen, [{'bar_theme': 'light', 'cmd': 'apply'}])
        self.assertFalse(Path(self.config).exists())

    def test_old_service_does_not_silently_drop_settings(self):
        thread = self.service([{'state': 'paused'}])
        with self.assertRaisesRegex(ValueError, 'reload'): p.save_preferences({'bar_theme': 'light'})
        thread.join(1)
        self.assertFalse(Path(self.config).exists())

    def test_service_error_is_reported_without_offline_write(self):
        thread = self.service([{'settings': {}}, {'error': 'Settings file is read-only.'}])
        with self.assertRaisesRegex(ValueError, 'read-only'): p.save_preferences({'bar_theme': 'light'})
        thread.join(1)
        self.assertFalse(Path(self.config).exists())


if __name__ == '__main__': unittest.main()
