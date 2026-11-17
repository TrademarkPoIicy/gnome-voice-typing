#!/usr/bin/env python3
"""Setup acknowledgement survives helper restarts; never installs anything."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('models_tool', ROOT / 'src/bin/models_tool.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class SetupStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status = Path(self.tmp.name) / '_deps.json'
        self.patch = patch.object(tool, 'STATUS', self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def helper(self):
        # New interpreter each time, just as when Settings reopens after a restart.
        subprocess.run([sys.executable, '-c',
                        'import runpy,sys; m=runpy.run_path(sys.argv[1]); '
                        'm["deps_ack"].__globals__["STATUS"]=sys.argv[2]; m["deps_ack"]()',
                        str(ROOT / 'src/bin/models_tool.py'), self.tmp.name], check=True)

    def test_ok_persists_across_processes(self):
        tool.write_status('_deps', status='done', msg='All set.')
        self.helper()
        self.assertIsNone(tool.read_status('_deps'))
        self.helper()  # repeated OK is harmless
        self.assertFalse(self.status.exists())

    def test_ok_preserves_failed_and_active_jobs(self):
        for state in ('failed', *tool.ACTIVE):
            tool.write_status('_deps', status=state)
            with patch.object(tool, '_pid_is_ours', return_value=True):
                tool.deps_ack()
                self.assertEqual(tool.read_status('_deps')['status'], state)

    def test_ok_does_not_reset_not_now(self):
        dismissed = Path(self.tmp.name) / 'dismissed.json'
        dismissed.write_text(json.dumps({'missing': ['uinput']}))
        with patch.object(tool, 'DISMISS', str(dismissed)):
            tool.write_status('_deps', status='done')
            tool.deps_ack()
        self.assertEqual(json.loads(dismissed.read_text()), {'missing': ['uinput']})


if __name__ == '__main__':
    unittest.main()
