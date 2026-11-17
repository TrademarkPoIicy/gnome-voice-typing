#!/usr/bin/env python3
import ast
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/bin'))
import customization as c


class FormattingTests(unittest.TestCase):
    def test_defaults_keep_existing_text(self):
        self.assertEqual(c.final_text("Hello, world!", {}), "Hello, world! ")
        self.assertFalse(c.whole_phrase_required({}))

    def test_spoken_marks_without_automatic_punctuation(self):
        cfg = {'punctuation': 'none', 'spoken_punctuation': True}
        self.assertEqual(c.final_text('Hello, comma, world period.', cfg), 'Hello, world. ')
        self.assertEqual(c.format_text('Really question mark?', cfg), 'Really?')

    def test_case_and_newlines(self):
        self.assertEqual(c.final_text('hello. new paragraph world!', {'capitalization': 'sentence'}), 'Hello\n\nWorld! ')
        self.assertEqual(c.final_text('New line.', {}), '\n')
        self.assertEqual(c.final_text('Hello!', {'capitalization': 'lower', 'trailing_space': False}), 'hello!')

    def test_vocab_boundaries_priority_and_no_cascade(self):
        rules = [{'heard': 'open ai', 'write': 'OpenAI'}, {'heard': 'open', 'write': 'close'}, {'heard': 'OpenAI', 'write': 'other'}]
        self.assertEqual(c.format_text('OPEN AI opened open.', {'vocabulary': rules, 'capitalization': 'lower'}), 'OpenAI opened close.')
        self.assertTrue(c.whole_phrase_required({'vocabulary': rules}))

    def test_invalid_rules_and_values(self):
        for values in ({'bar_opacity': 60}, {'bar_opacity': True}, {'show_model': 'false'}, {'bar_theme': 'invalid'},
                       {'vocabulary': [{'heard': 'a', 'write': ''}]}, {'vocabulary': [{'heard': 'a', 'write': 'b'}, {'heard': 'A', 'write': 'c'}]}):
            with self.assertRaises(ValueError): c.validate(values)
        self.assertEqual(c.settings({'bar_theme': 'invalid'})['bar_theme'], 'dark')

    def test_merge_preserves_unrelated_settings_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'config.json')
            c.update_config(path, {'model': 'keep-me'})
            c.update_config(path, {'bar_theme': 'light'})
            self.assertEqual(json.loads(Path(path).read_text()), {'model': 'keep-me', 'bar_theme': 'light'})
            Path(path).write_text('{broken')
            with self.assertRaises(ValueError): c.update_config(path, {'bar_theme': 'dark'})
            self.assertEqual(Path(path).read_text(), '{broken')

    def test_concurrent_writes_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'config.json')
            threads = [threading.Thread(target=c.update_config, args=(path, {str(i): i})) for i in range(12)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(len(json.loads(Path(path).read_text())), 12)

    def test_daemon_uses_formatted_text_and_tracks_undo_length(self):
        # Load actual methods without starting hardware threads or importing ASR packages.
        tree = ast.parse((ROOT / 'src/bin/dictated.py').read_text())
        names = {'postprocess', 'norm'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        daemon = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Daemon')
        methods = [n for n in daemon.body if isinstance(n, ast.FunctionDef) and n.name in ('phrase', '_stream_mode')]
        import re, time
        cfg = {'vocabulary': [{'heard': 'open ai', 'write': 'OpenAI'}], 'trailing_space': False}
        ns = dict(customization=c, CFG=cfg, re=re, time=time, ASR_LOCK=threading.Lock(), RATE=16000,
                  DRY=False, log=lambda *a: None, HALLUC=set(), CMD_PAUSE=set(), CMD_CLOSE=set(), CMD_DELETE=set())
        exec(compile(ast.Module(body=nodes + methods, type_ignores=[]), '<daemon methods>', 'exec'), ns)
        obj = Mock(asr=Mock(transcribe=Mock(return_value='open ai.')), stream_block_until=0)
        obj._stream_mode = lambda: ns['_stream_mode'](obj)
        self.assertEqual(obj._stream_mode(), 'off')
        session = Mock(first=True, prefix='')
        ns['phrase'](obj, [0] * 32000, session)
        obj.typer.type.assert_called_once_with('OpenAI.')
        self.assertEqual(obj.last_typed, 'OpenAI.')


if __name__ == '__main__': unittest.main()
