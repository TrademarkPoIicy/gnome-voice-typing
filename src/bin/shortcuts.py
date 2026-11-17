#!/usr/bin/python3
"""JSON bridge for the in-panel shortcut editor (system Gio/GTK, no window)."""
import json
from pathlib import Path
import sys
N = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(N / 'lib'))
import gvt_shortcuts

try:
    if len(sys.argv) > 1:
        gvt_shortcuts.save(json.loads(sys.argv[1]), str(N / 'install-state.json'), str(N / 'bin/voice-typing'))
    print(json.dumps({'ok': True, 'bindings': gvt_shortcuts.current()}))
except Exception as e:
    print(json.dumps({'error': str(e)}))
    sys.exit(1)
