#!/usr/bin/python3
"""One-shot AT-SPI probe for the overlay: prints JSON describing the focused text field of the ACTIVE window, or {} if unknown.
Extents are in WINDOW coordinates (Wayland clients cannot know screen coords); the overlay adds the window origin from mutter.
Reads only geometry (never text contents). Never changes focus. Usage: focusbox.py [time-budget-seconds]"""
import sys, time, json
try:
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
except Exception:
    print('{}'); sys.exit(0)
budget = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
T0 = time.time()
def left(): return budget - (time.time() - T0)
try:
    Atspi.init()
    try: Atspi.set_timeout(250, 600)
    except Exception: pass
    desk = Atspi.get_desktop(0)
except Exception:
    print('{}'); sys.exit(0)
S = Atspi.StateType
def has(a, st):
    try: return a.get_state_set().contains(st)
    except Exception: return False
def kids(a):
    try:
        n = a.get_child_count()
        return [a.get_child_at_index(i) for i in range(min(n, 60))]
    except Exception: return []
def find_active():
    for app in kids(desk):
        for w in kids(app):
            if w is not None and has(w, S.ACTIVE): return app, w
    return None, None
def chain(a):
    p = []
    try:
        while a is not None and a.get_role() != Atspi.Role.APPLICATION:
            p.append(a.get_index_in_parent()); a = a.get_parent()
    except Exception: pass
    return '/'.join(str(i) for i in reversed(p))
def probe(node):
    comp = node.get_component_iface()
    if comp is None: return None
    e = comp.get_extents(Atspi.CoordType.WINDOW)
    if e.width <= 0 or e.height <= 0: return None
    out = {'field': [e.x, e.y, e.width, e.height], 'caret': None, 'coord': 'window'}
    try:
        t = node.get_text_iface()
        if t is not None:
            n = t.get_character_count(); c = t.get_caret_offset()
            if n > 0 and c >= 0:
                off = c if c < n else n - 1
                r = t.get_character_extents(off, Atspi.CoordType.WINDOW)
                if r.height > 0 and (r.x or r.y or r.width):
                    out['caret'] = [r.x + (r.width if c >= n else 0), r.y, r.height]
    except Exception: pass
    return out
def main():
    app, win = find_active()
    if win is None: return {}
    best = None; q = [win]; seen = 0
    while q and left() > 0 and seen < 6000:
        a = q.pop(0); seen += 1
        if has(a, S.FOCUSED) and a is not win:
            texty = has(a, S.EDITABLE) or has(a, S.SINGLE_LINE) or has(a, S.MULTI_LINE)
            if texty:
                r = probe(a)
                if r: r['key'] = (app.get_name() or '?') + ':' + chain(a); return r
            elif best is None: best = a
        q.extend(k for k in kids(a) if k is not None)
    if best is not None:
        r = probe(best)
        if r: r['key'] = (app.get_name() or '?') + ':' + chain(best); return r
    return {}
try: print(json.dumps(main()))
except Exception: print('{}')
