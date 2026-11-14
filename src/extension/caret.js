// caret.js - focused text caret / widget locator for the voice typing overlay (GNOME Shell 51, ES module).
//
// USAGE (from extension.js):
//     import * as Caret from './caret.js';
//     Caret.init();                       // optional: getFocusRect() lazily inits too
//     const r = Caret.getFocusRect();     // cheap, synchronous; safe to call from a timer
//     Caret.destroy();                    // in disable(): removes listeners, drops the cache
//
// API
//     init(): boolean
//         Starts AT-SPI initialisation (asynchronous, idempotent; true = started/ready) and registers an Atspi.EventListener for
//         'object:state-changed:focused' (+ legacy 'focus:' and 'window:deactivate') that caches the
//         currently focused accessible. Returns true when listening. Never throws.
//     isReady(): boolean   true once the listeners are registered.
//     seedFocus(pid?): boolean
//         One bounded search (depth/node/time capped) for the field that was already focused when we started listening or when the
//         window was re-activated (no focus event arrives for those). Runs automatically once after init; extension.js also calls it on every show.
//     destroy(): void
//         Deregisters the listeners and clears the cache. Safe to call repeatedly / without init().
//     getFocusRect(): {x:number, y:number, w:number, h:number, kind:'caret'|'widget'} | null
//         Screen coordinates (Atspi.CoordType.SCREEN), integers.
//           kind 'caret'  : rect of the character at the caret offset. At end-of-text (or on a
//                           newline/empty line) the previous character is used and the rect is
//                           collapsed to its right edge (w = 0), i.e. where the caret really is.
//           kind 'widget' : no usable caret (no Text interface / extents unsupported), so the
//                           focused widget's Component extents are returned instead.
//         null            : nothing cached/focused, accessibility unavailable, the focused object
//                           belongs to this process (gnome-shell itself), or the app exposes
//                           nothing usable (e.g. some Electron apps without --force-renderer-accessibility).
//
// DESIGN NOTES
//   * Cost per call = at most a few D-Bus round trips to the ONE focused app (caret offset,
//     character count, character extents). The tree is never walked. Focus changes arrive as events.
//   * Atspi.set_timeout() caps how long a hung app can stall a call (see TIMEOUT_MS below).
//   * Calls into an app are synchronous D-Bus calls on the caller's thread. Objects owned by
//     gnome-shell itself are skipped, because calling ourselves would deadlock the compositor.
//   * REQUIREMENT: toolkits only talk AT-SPI when accessibility is enabled for the session:
//         gsettings set org.gnome.desktop.interface toolkit-accessibility true
//     (this module neither reads nor changes it; apps started before it was enabled must be restarted).
//   * WAYLAND: apps cannot know their global position, and GTK4 answers CoordType.SCREEN with (0,0)-based
//     numbers. Inside gnome-shell this module therefore also reads CoordType.WINDOW extents and adds the
//     origin of global.display.focus_window's frame rect (verified in the nested-session QA with a GTK4
//     entry). Outside gnome-shell (no `global`) the raw SCREEN rect is returned unmapped.
//   * GTK4 text widgets do not implement get_character_extents (error), so they yield kind 'widget'.
//   * init() is asynchronous (see below): getFocusRect() is null until isReady() is true, and the
//     focused-widget cache only fills on a focus-change event after that (nothing is known for a field
//     that was already focused before init).

import Atspi from 'gi://Atspi';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

const TIMEOUT_MS = 250;        // per D-Bus call to the focused app
const STARTUP_TIMEOUT_MS = 1000;
const BUS_TIMEOUT_MS = 1000;   // org.a11y.Bus reachability check

let _inited = false;
let _failed = false;
let _pending = false;
let _tok = 0;
let _listeners = [];       // [[Atspi.EventListener, event-name], ...]
let _focused = null;           // cached Atspi.Accessible
let _ownPid = null;
let _fid = 0;                  // bumps each time a different accessible gains focus (lets the shell tell 'typing' from 'moved to another field')
let _focusedPid = -1;
let _focusCb = null;           // optional: called (cheaply, never throws) after a focus gain

function _getOwnPid() {
    if (_ownPid === null) {
        try { _ownPid = parseInt(GLib.file_read_link('/proc/self'), 10) || -1; }
        catch (e) { _ownPid = -1; }
    }
    return _ownPid;
}

function _isOwn(acc) {
    try {
        const pid = acc.get_process_id();       // -1 on error
        return pid > 0 && pid === _getOwnPid();
    } catch (e) { return false; }
}

function _onFocus(event) {
    try {
        // state-changed:focused -> detail1 = 1 gained, 0 lost; legacy 'focus:' has no detail
        const src = event.source;
        if (!src) return;
        const gained = event.type.startsWith('focus:') || event.detail1 === 1;
        if (gained) {
            _focused = _isOwn(src) ? null : src;
            if (_focused) {
                _fid++;
                try { _focusedPid = src.get_process_id(); } catch (e) { _focusedPid = -1; }
                if (_focusCb) { try { _focusCb(); } catch (e) { /* ignore */ } }
            }
        } else if (_focused && src === _focused) {
            _focused = null;
        }
    } catch (e) { /* never propagate into the shell main loop */ }
}

function _onWindowDeactivate(_event) {
    _focused = null;
}

// init() NEVER calls Atspi.init() until it has proven, asynchronously, that the accessibility bus can be
// reached: libatspi g_error()s (aborts the whole process = gnome-shell) when it cannot connect. So:
//   1. async org.a11y.Bus.GetAddress on the session bus (short timeout),
//   2. async connect to the returned address (then closed again),
//   3. only then Atspi.init() + listeners, all inside try/catch.
// init() therefore returns immediately; getFocusRect() returns null until isReady() is true.
export function init() {
    if (_inited || _pending) return true;
    if (_failed) return false;
    try {
        _pending = true;
        const tok = ++_tok;
        Gio.DBus.session.call('org.a11y.Bus', '/org/a11y/bus', 'org.a11y.Bus', 'GetAddress', null,
            new GLib.VariantType('(s)'), Gio.DBusCallFlags.NONE, BUS_TIMEOUT_MS, null, (conn, res) => {
                let addr = null;
                try { addr = conn.call_finish(res).deepUnpack()[0]; } catch (e) { addr = null; }
                if (tok !== _tok || !addr) { _pending = false; return; }
                try {
                    Gio.DBusConnection.new_for_address(addr,
                        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                        null, null, (_o, res2) => {
                            let c = null;
                            try { c = Gio.DBusConnection.new_for_address_finish(res2); } catch (e) { c = null; }
                            if (c) { try { c.close(null, null); } catch (e) { /* ignore */ } }
                            _pending = false;
                            if (tok !== _tok || !c) return;
                            _startAtspi();
                        });
                } catch (e) { _pending = false; }
            });
        return true;
    } catch (e) {
        _pending = false;
        return false;
    }
}

export function isReady() {
    return _inited;
}

function _startAtspi() {
    try {
        Atspi.init();               // 0 on first init, 1 if already inited - both fine
        try { Atspi.set_timeout(TIMEOUT_MS, STARTUP_TIMEOUT_MS); } catch (e) { /* ignore */ }
        for (const [key, cb] of [['object:state-changed:focused', _onFocus],
                                 ['focus:', _onFocus],
                                 ['window:deactivate', _onWindowDeactivate]]) {
            try {
                const l = Atspi.EventListener.new(cb);
                l.register(key);
                _listeners.push([l, key]);
            } catch (e) { /* this event kind unavailable; keep the others */ }
        }
        _inited = true;
        // a field that was already focused before we started listening never sends an event: look it up once, soon (never inside this callback)
        // (the first AT-SPI calls after init can be slow and eat the time budget, so retry shortly if nothing was found and no event arrived meanwhile)
        const tok = _tok;
        for (const ms of [50, 700, 2500]) {
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
                try { if (tok === _tok && _inited && !_focused) seedFocus(); } catch (e) { /* ignore */ }
                return GLib.SOURCE_REMOVE;
            });
        }
    } catch (e) {
        _failed = true;
        _cleanup();
    }
}

// ---- seedFocus(): one bounded lookup of the field that is ALREADY focused (no event will ever arrive for it) ----
// Matches the focused Meta.Window's pid to an AT-SPI application, takes its STATE_ACTIVE frame(s) and searches them depth-first for the
// STATE_FOCUSED descendant. Bounded by node count, depth and a time budget; never walks the desktop (only app roots are pid-compared).
const SEED_MAX_NODES = 600;
const SEED_MAX_DEPTH = 30;
const SEED_BUDGET_US = 150000;
function _shellFocusPid() {
    try { if (typeof global === 'undefined') return -1; const w = global.display.focus_window; return w ? w.get_pid() : -1; }
    catch (e) { return -1; }
}
function _findFocused(root, over) {
    const SS = Atspi.StateType;
    let nodes = 0;
    const stack = [[root, 0]];
    while (stack.length) {
        if (over() || nodes >= SEED_MAX_NODES) return null;
        const [a, d] = stack.pop();
        nodes++;
        let n = 0;
        try {
            a.clear_cache();                       // we only listen for 'focused' events, so cached states can be stale
            const ss = a.get_state_set();
            if (a !== root) {
                if (ss.contains(SS.FOCUSED)) return a;
                if (!ss.contains(SS.SHOWING)) continue;      // prune hidden subtrees
            }
            n = d < SEED_MAX_DEPTH ? Math.min(a.get_child_count(), 200) : 0;
        } catch (e) { continue; }
        for (let i = n - 1; i >= 0; i--) {
            try { const ch = a.get_child_at_index(i); if (ch) stack.push([ch, d + 1]); } catch (e) { /* skip */ }
        }
    }
    return null;
}
// pid: the focused window's pid (default: global.display.focus_window). Returns true when the cache now holds a focused field.
export function seedFocus(pid) {
    try {
        if (!_inited) return false;
        if (pid === undefined || pid === null) pid = _shellFocusPid();
        if (!(pid > 0) || pid === _getOwnPid()) return false;
        if (!GLib.file_test('/proc/' + pid, GLib.FileTest.EXISTS)) return false;
        const t0 = GLib.get_monotonic_time();
        const over = () => GLib.get_monotonic_time() - t0 > SEED_BUDGET_US;
        const desk = Atspi.get_desktop(0);
        const na = Math.min(desk.get_child_count(), 64);
        for (let i = 0; i < na && !over(); i++) {
            let app = null;
            try { app = desk.get_child_at_index(i); if (!app || app.get_process_id() !== pid) continue; } catch (e) { continue; }
            let nf = 0;
            try { nf = Math.min(app.get_child_count(), 32); } catch (e) { continue; }
            for (let j = 0; j < nf && !over(); j++) {
                try {
                    const fr = app.get_child_at_index(j);
                    if (!fr) continue;
                    fr.clear_cache();
                    if (!fr.get_state_set().contains(Atspi.StateType.ACTIVE)) continue;
                    const hit = _findFocused(fr, over);
                    if (hit) {
                        if (hit !== _focused) { _focused = hit; _focusedPid = pid; _fid++; }
                        return true;
                    }
                } catch (e) { /* next frame */ }
            }
        }
    } catch (e) { /* never propagate into the shell */ }
    return false;
}

function _cleanup() {
    for (const [l, ev] of _listeners) {
        try { l.deregister(ev); } catch (e) { /* ignore */ }
    }
    _listeners = [];
    _focused = null;
    _orgFor = null;
}

export function destroy() {
    try { _tok++; _pending = false; _cleanup(); } catch (e) { /* ignore */ }
    _inited = false;
    _failed = false;                // allow a later init() to retry
}

function _validRect(r) {
    return !!r && r.width >= 0 && r.height > 0 && r.x > -32768 && r.y > -32768 &&
        !(r.x === 0 && r.y === 0 && r.width === 0 && r.height === 0) &&
        !(r.x === -1 && r.y === -1);
}

function _ext(fn, coord) {
    try {
        const r = fn(coord);
        return _validRect(r) ? {x: r.x, y: r.y, w: r.width, h: r.height} : null;
    } catch (e) { return null; }
}

// Frame rect of the window mutter says has focus (only available inside gnome-shell).
function _focusFrame() {
    try {
        const win = global.display.focus_window;
        if (!win) return null;
        const f = win.get_frame_rect();
        return {x: f.x, y: f.y, w: f.width, h: f.height};
    } catch (e) { return null; }     // not running inside gnome-shell
}

// Pick the real screen-space rect from the SCREEN-coord (rs) and WINDOW-coord (rw) answers.
// Wayland clients cannot know their global position: GTK4 answers SCREEN with window-relative / (0,0)
// numbers, while WINDOW coordinates (relative to the toplevel's frame) are right. X11/Xwayland clients
// answer SCREEN correctly. So: trust rs only if it lies inside the focused window's frame AND is not
// just a copy of rw; otherwise map rw: WINDOW coords are relative to the toplevel SURFACE, and where the frame sits
// inside that surface differs per toolkit (GTK4: frame at 0,0; Firefox: frame at the CSD shadow offset, e.g. 26,23),
// so `org` = the toplevel frame accessible's own WINDOW-coord position (see _topOrigin) is subtracted first. Outside gnome-shell (no frame) rs is returned raw.
function _resolve(rs, rw, org) {
    const fr = _focusFrame();
    if (!fr) return rs;
    const inside = r => r.x >= fr.x - 2 && r.x <= fr.x + fr.w + 2 && r.y >= fr.y - 2 && r.y <= fr.y + fr.h + 2;
    const sameAsWin = rs && rw && rs.x === rw.x && rs.y === rw.y && (fr.x !== 0 || fr.y !== 0);
    if (rs && inside(rs) && !sameAsWin) return rs;
    if (rw) {
        const c = {x: rw.x - org.x + fr.x, y: rw.y - org.y + fr.y, w: rw.w, h: rw.h};
        if (inside(c)) return c;
    }
    return null;
}

const TOP_ROLES = new Set([Atspi.Role.FRAME, Atspi.Role.WINDOW, Atspi.Role.DIALOG, Atspi.Role.ALERT, Atspi.Role.FILE_CHOOSER]);
let _orgFor = null, _org = {x: 0, y: 0};
// Position of the focused object's toplevel frame inside the surface coordinate space (cached per focused object).
function _topOrigin(acc) {
    if (_orgFor === acc) return _org;
    _orgFor = acc; _org = {x: 0, y: 0};
    try {
        let p = acc;
        for (let i = 0; i < 30 && p; i++) {
            if (TOP_ROLES.has(p.get_role())) {
                const r = p.get_component_iface().get_extents(Atspi.CoordType.WINDOW);
                if (r && r.width > 0 && r.height > 0) _org = {x: r.x, y: r.y};
                break;
            }
            p = p.get_parent();
        }
    } catch (e) { /* keep 0,0 */ }
    return _org;
}

function _caretRect(acc) {
    let text;
    try { text = acc.get_text_iface(); } catch (e) { return null; }
    if (!text) return null;
    let off, count;
    try {
        off = text.get_caret_offset();
        count = text.get_character_count();
    } catch (e) { return null; }
    if (off < 0) return null;

    // candidates: [char offset, caret sits after that char]
    const cands = off < count ? [[off, false], [off - 1, true]] : (count > 0 ? [[count - 1, true]] : []);
    for (const [o, atEnd] of cands) {
        if (o < 0) continue;
        const rs = _ext(c => text.get_character_extents(o, c), Atspi.CoordType.SCREEN);
        const rw = _ext(c => text.get_character_extents(o, c), Atspi.CoordType.WINDOW);
        if (!rs && !rw) continue;
        const r = _resolve(rs, rw, _topOrigin(acc));
        if (!r) continue;
        return {
            x: Math.round(atEnd ? r.x + r.w : r.x), y: Math.round(r.y),
            w: atEnd ? 0 : Math.round(r.w), h: Math.round(r.h), kind: 'caret',
        };
    }
    return null;
}

function _widgetRect(acc) {
    let comp;
    try { comp = acc.get_component_iface(); } catch (e) { return null; }
    if (!comp) return null;
    const rs = _ext(c => comp.get_extents(c), Atspi.CoordType.SCREEN);
    const rw = _ext(c => comp.get_extents(c), Atspi.CoordType.WINDOW);
    const r = _resolve(rs, rw, _topOrigin(acc));
    if (!r || r.w <= 0) return null;
    // a half-screen-sized 'widget' is a document canvas / terminal surface (e.g. LibreOffice's document focus), not a text box: let the shell use its other probes
    if (r.w >= 800 && r.h >= 500) return null;
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h), kind: 'widget'};
}

export function getFocusRect() {
    try {
        if (!_inited) { init(); return null; }   // lazy init is asynchronous; null until ready
        const acc = _focused;
        if (!acc) return null;
        // the app may have exited without a focus-lost event: touching its disposed accessible logs a Gjs-CRITICAL, so check the pid first
        if (_focusedPid > 0 && !GLib.file_test('/proc/' + _focusedPid, GLib.FileTest.EXISTS)) { _focused = null; return null; }
        let r = _caretRect(acc) ?? _widgetRect(acc);
        if (!r) r = _caretRect(acc) ?? _widgetRect(acc);   // the very first extents call on a freshly seeded accessible can come back empty; one retry is cheap
        if (r) r.fid = _fid;
        return r;
    } catch (e) {
        return null;
    }
}

// The shell registers one callback to re-probe soon after focus moves to a different field.
export function setFocusCallback(cb) {
    _focusCb = (typeof cb === 'function') ? cb : null;
}

// Diagnostics only: role/name/pid of the cached focused accessible (or null).
export function describeFocus() {
    try {
        const a = _focused;
        return a ? {role: a.get_role_name(), name: a.get_name(), pid: a.get_process_id(), org: _org} : null;
    } catch (e) { return null; }
}
