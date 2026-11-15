import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Voice typing bar: a compact floating pill. Shell chrome only: never grabs the keyboard, never takes typing focus.
// v2: draggable bar (position remembered), hover/pressed states on every control, red-glow close buttons,
//     Save button with real states (dimmed -> active on change -> pressed -> "Saved" -> dimmed; reset on close).
const RT = GLib.get_user_runtime_dir();
const DIR = RT + '/gnome-voice-typing-overlay';
const ACCENT = '#4cc2ff', ACCENT_DIM = 'rgba(76,194,255,0.30)', PANEL_BG = 'rgba(43,43,43,0.985)';
const FONT = 'font-family: Inter, Cantarell, Noto Sans, sans-serif;';   // only the FIRST family may be quoted in St CSS; later quoted names trigger "Couldn't parse family in font property"
const PAUSES = [[10, '10 s'], [30, '30 s'], [60, '1 min'], [120, '2 min'], [0, 'Never']];
const BAR_W = 204, PANEL_W = 460, MARGIN = 8;   // compact bar
const IDLE_BG = 'rgba(255,255,255,0.10)';   // neutral mic circle when idle / paused
const CLOSE_RED = '#c42b1c', CLOSE_RED_DOWN = '#9d2216', CLOSE_GLOW = 'rgba(232,40,28,0.75)';

const devOf = (e) => (e && e.device) || (e && e.backend === 'vk' ? 'gpu' : 'cpu');
const DEV_LABEL = {gpu: 'Graphics card', cpu: 'Processor'};
const PAGES = [['model', 'Speech model', 'audio-input-microphone-symbolic'], ['mic', 'Microphone & noise', 'audio-volume-high-symbolic'],
               ['typing', 'Typing & sounds', 'input-keyboard-symbolic'], ['setup', 'Setup & uninstall', 'emblem-system-symbolic']];
const shortName = (n) => (n || '').replace(/\s*\(.*?\)/g, '').replace(/-TDT.*$/, '').replace(/ q5$/, '').trim();

const SEC = 'rgba(255,255,255,0.74)';      // secondary text on the dark panel (~8:1 contrast)
function wrapLbl(l) { const t = l.clutter_text; t.line_wrap = true; t.line_wrap_mode = Pango.WrapMode.WORD_CHAR; t.ellipsize = Pango.EllipsizeMode.NONE; l.x_expand = true; return l; }

function lbl(text, size, color, weight) {
    return new St.Label({text, style: `${FONT} font-size: ${size}pt; color: ${color};${weight ? ' font-weight: ' + weight + ';' : ''}`});
}

export default class VoiceTypingOverlay extends Extension {
    enable() {
        GLib.mkdir_with_parents(DIR, 0o700);
        this._state = 'hidden'; this._last = 0; this._info = {}; this._models = null; this._panelOpen = false;
        this._sel = {model: '', pause: 60, mic: '', sounds: true, dev: 'gpu'}; this._base = Object.assign({}, this._sel);
        this._spinners = []; this._spinId = 0; this._geomKey = ''; this._placed = false; this._dragPos = null; this._curKey = null;
        this._saveState = 'idle'; this._saveTimer = 0; this._readyUntil = 0; this._readyTimer = 0; this._wasLoading = false;
        this._dragging = false; this._capId = 0; this._grab = null; this._panelIdle = 0; this._locTok = 0; this._rawPrev = 'hidden'; this._locating = false; this._following = false;
        this._field = null; this._target = null; this._followId = 0; this._fsTimer = 0; this._focusSig = 0; this._userMoved = false;
        // one-click model downloads: catalog (every model + its state) comes from `voice-typing-models status --json`, progress from models/.status/<id>.json
        this._cat = null; this._dlW = {}; this._pend = {}; this._confirm = ''; this._note = {}; this._dlTimer = 0; this._catTimer = 0; this._adopted = false; this._catSig = ''; this._scrollKeep = 0;
        // first-launch check for missing system packages (graphics-card engine / typing): `voice-typing-models deps --json`, progress in models/.status/_deps.json
        this._deps = null; this._depsChecked = false; this._depsTimer = 0; this._depsPend = 0; this._depsOpen = false;

        // ---- main bar ----
        this._bar = new St.BoxLayout({
            reactive: true, can_focus: false, track_hover: true, visible: false, opacity: 0, width: BAR_W,
            style: this._barStyle(false),
        });
        this._bar.connect('notify::hover', () => { if (this._bar) this._bar.style = this._barStyle(this._bar.hover || this._dragging); });
        this._bar.connect('button-press-event', (a, ev) => this._dragBegin(ev));
        this._grip = new St.Icon({icon_name: 'list-drag-handle-symbolic', icon_size: 10, y_align: Clutter.ActorAlign.CENTER,
                                  style: 'color: rgba(255,255,255,0.28);'});
        this._mic = new St.Bin({width: 24, height: 24, y_align: Clutter.ActorAlign.CENTER,
                                style: `background-color: ${ACCENT}; border-radius: 12px;`});
        this._micIcon = new St.Icon({icon_name: 'audio-input-microphone-symbolic', icon_size: 12, style: 'color: #000;',
                                     x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER});
        this._mic.set_child(this._micIcon);
        this._micS = {bg: ACCENT, icon: 'audio-input-microphone-symbolic', fg: '#000', ring: 0, dim: false};
        this._mic.set_pivot_point(0.5, 0.5);
        // clicking the mic = the hotkey (toggle listening / paused; wakes the daemon). Chrome click: no focus change.
        this._interactive(this._mic, (a, st) => this._micStyle(st), () => this._cli(['toggle']), () => !(this._info && this._info.loading));
        const texts = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, y_align: Clutter.ActorAlign.CENTER, x_expand: true, style: 'spacing: 0px;'});
        this._label = lbl('Listening…', 9, 'rgba(255,255,255,0.92)');
        this._label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._modelLabel = lbl('', 6.5, 'rgba(255,255,255,0.55)');
        this._modelLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        texts.add_child(this._label); texts.add_child(this._modelLabel);
        this._barSpin = this._mkSpinner(12); this._barSpin.visible = false;
        this._gear = this._iconButton('emblem-system-symbolic', 12, false, () => this._togglePanel(), 24);
        this._close = this._iconButton('window-close-symbolic', 12, true, () => this._cli(['close']), 24);
        for (const a of [this._grip, this._mic, texts, this._barSpin, this._gear, this._close]) this._bar.add_child(a);
        Main.layoutManager.addChrome(this._bar, {trackFullscreen: false});

        // ---- settings panel ----
        this._panel = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, visible: false, opacity: 0, reactive: false, width: PANEL_W,
            style: `background-color: ${PANEL_BG}; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px 16px 16px; spacing: 4px; ` +
                   `box-shadow: 0 8px 28px rgba(0,0,0,0.55), 0 2px 6px rgba(0,0,0,0.35); ${FONT}`});
        Main.layoutManager.addChrome(this._panel, {trackFullscreen: false});

        this._mon = Gio.File.new_for_path(DIR).monitor_directory(Gio.FileMonitorFlags.NONE, null);
        this._monId = this._mon.connect('changed', (m, file, other, ev) => {
            if (ev !== Gio.FileMonitorEvent.CHANGES_DONE_HINT && ev !== Gio.FileMonitorEvent.MOVED_IN && ev !== Gio.FileMonitorEvent.RENAMED) return;
            const n = file.get_basename();
            if (n === 'state.json') this._read();
            else if (n === 'models.json') this._readModels();
        });
        this._watch = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => { this._watchdog(); return GLib.SOURCE_CONTINUE; });
        this._readModels();
        // optional in-shell AT-SPI locator (caret.js: getFocusRect() -> {x, y, w, h} | null); absent -> bin/focusbox.py probe, then saved position
        this._caret = null;
        // caret.js's init() aborts gnome-shell (atspi g_error) when the accessibility bus is unreachable, so only load it once org.a11y.Bus answers
        Gio.DBus.session.call('org.a11y.Bus', '/org/a11y/bus', 'org.a11y.Bus', 'GetAddress', null, new GLib.VariantType('(s)'),
            Gio.DBusCallFlags.NONE, 1500, null, (conn, res) => {
                try { conn.call_finish(res); } catch (e) { return; }
                if (!this._bar) return;
                import(`file://${this.path}/caret.js`).then(m => { try { m.init(); } catch (e) {} if (this._bar) { this._caret = m; try { m.setFocusCallback(() => this._focusPoke()); } catch (e) {} } else { try { m.destroy(); } catch (e) {} } }).catch(() => {});
            });
        GLib.file_set_contents(DIR + '/ready', '1');
    }

    disable() {
        this._dragEnd(false); this._stopFollow(); this._locTok++;
        if (this._caret) { try { this._caret.setFocusCallback(null); } catch (e) {} try { this._caret.destroy(); } catch (e) {} this._caret = null; }
        if (this._monId) { this._mon.disconnect(this._monId); this._mon.cancel(); this._monId = 0; }
        for (const k of ['_watch', '_spinId', '_saveTimer', '_readyTimer', '_panelIdle', '_dlTimer', '_catTimer', '_depsTimer']) if (this[k]) { GLib.source_remove(this[k]); this[k] = 0; }
        try { Gio.File.new_for_path(DIR + '/ready').delete(null); } catch (e) {}
        for (const a of [this._bar, this._panel]) if (a) { Main.layoutManager.removeChrome(a); a.destroy(); }
        this._bar = this._panel = null;
    }

    // ---------- generic controls ----------
    // Makes an actor behave like a real button: hover / pressed / disabled styling via styleFn(actor, {hover, down, enabled}),
    // click fires on release while still hovered. Never takes key focus.
    _interactive(actor, styleFn, onClick, enabledFn) {
        actor.reactive = true; actor.can_focus = false; actor.track_hover = true;
        let down = false;
        const en = () => (enabledFn ? enabledFn() : true);
        const upd = () => styleFn(actor, {hover: actor.hover && en(), down: down && en(), enabled: en()});
        actor.connect('notify::hover', () => { if (!actor.hover) down = false; upd(); });
        actor.connect('button-press-event', (a, ev) => { if (ev.get_button() !== 1) return Clutter.EVENT_PROPAGATE; down = true; upd(); return Clutter.EVENT_STOP; });
        actor.connect('button-release-event', (a, ev) => {
            const was = down; down = false; upd();
            if (was && actor.hover && en()) onClick();
            return Clutter.EVENT_STOP;
        });
        actor._upd = upd; upd();
        return actor;
    }

    _iconButton(iconName, size, danger, onClick, bin = 32) {
        const icon = new St.Icon({icon_name: iconName, icon_size: size, x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER});
        const b = new St.Bin({width: bin, height: bin, y_align: Clutter.ActorAlign.CENTER, child: icon});
        const R = bin > 28 ? 8 : 12;   // bar buttons (24 px) are round, panel close (32 px) is a soft square
        return this._interactive(b, (a, s) => {
            if (danger && s.down) { a.style = `border-radius: ${R}px; background-color: ${CLOSE_RED_DOWN};`; icon.style = 'color: #fff;'; }
            else if (danger && s.hover) { a.style = `border-radius: ${R}px; background-color: ${CLOSE_RED}; box-shadow: 0 0 12px 1px ${CLOSE_GLOW};`; icon.style = 'color: #fff;'; }
            else if (s.down) { a.style = `border-radius: ${R}px; background-color: rgba(255,255,255,0.04);`; icon.style = 'color: rgba(255,255,255,0.6);'; }
            else if (s.hover) { a.style = `border-radius: ${R}px; background-color: rgba(255,255,255,0.09);`; icon.style = 'color: rgba(255,255,255,0.95);'; }
            else { a.style = `border-radius: ${R}px; background-color: transparent;`; icon.style = 'color: rgba(255,255,255,0.68);'; }
        }, onClick);
    }

    _barStyle(hot) {
        return `background-color: rgba(32,32,32,0.97); border: 1px solid ${hot ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.08)'}; border-radius: 22px; ` +
               'padding: 4px 5px 4px 9px; spacing: 4px; box-shadow: 0 4px 14px rgba(0,0,0,0.50), 0 1px 3px rgba(0,0,0,0.35);';
    }

    _mkSpinner(size) {
        const w = new St.Widget({width: size, height: size, y_align: Clutter.ActorAlign.CENTER,
            style: `border: ${size <= 14 ? 2 : 3}px solid rgba(255,255,255,0.18); border-top-color: ${ACCENT}; border-radius: ${size}px;`});
        w.set_pivot_point(0.5, 0.5);
        this._spinners.push(w); return w;
    }
    _spinTick() {
        const dj = this._deps && this._deps.job && this._deps.job.status;
        const loading = !!this._info.loading || (this._panelOpen && ['installing', 'building', 'downloading', 'verifying'].includes(dj));
        if (!loading) { this._spinId = 0; for (const w of this._spinners) w.rotation_angle_z = 0; return GLib.SOURCE_REMOVE; }
        for (const w of this._spinners) w.rotation_angle_z = (w.rotation_angle_z + 24) % 360;
        return GLib.SOURCE_CONTINUE;
    }
    _ensureSpin() { if (this._info.loading && !this._spinId) this._spinId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => this._spinTick()); }

    _cli(args) {
        const cli = (this._models && this._models.cli) || null;
        if (!cli) return;
        try { Gio.Subprocess.new([cli, ...args], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE); } catch (e) { console.error('voice-typing cli: ' + e); }
    }

    _read() {
        let d;
        try {
            const [ok, bytes] = GLib.file_get_contents(DIR + '/state.json');
            if (!ok || !bytes.length) return;
            d = JSON.parse(new TextDecoder().decode(bytes));
        } catch (e) { return; }
        this._last = GLib.get_monotonic_time();
        this._info = d;
        this._apply(d.state, d.level || 0, d.text || '', !!d.busy);
        this._updateNoiseMeter();
    }

    _readModels() {
        try {
            const [ok, bytes] = GLib.file_get_contents(DIR + '/models.json');
            if (!ok || !bytes.length) return;
            const m = JSON.parse(new TextDecoder().decode(bytes));
            const first = !this._models;
            this._models = m;
            if (first || !this._panelOpen) this._syncSel();
            if (this._panelOpen) this._buildPanel();
        } catch (e) {}
    }
    _syncSel() {
        const m = this._models; if (!m) return;
        this._sel = {model: m.current, pause: m.settings.auto_pause_seconds, mic: m.settings.mic || '', sounds: m.settings.sounds !== false,
                     dev: devOf(this._entries().find(e => e.id === m.current) || (m.models || []).find(e => e.id === m.current) || {device: m.settings.device})};
        this._base = Object.assign({}, this._sel);
    }
    _pickDevice(dev) {   // switching device pre-selects the equivalent model there, else the device's recommended default
        const m = this._models; if (!m || this._sel.dev === dev) return;
        const inDev = this._entries().filter(e => e.state === 'installed' && devOf(e) === dev);
        if (!inDev.length) { this._sel.dev = dev; this._buildPanel(); return; }     // nothing installed for that device yet: just show its list (Download buttons)
        let id = (m.equiv || {})[this._sel.model];
        if (!id || !inDev.find(e => e.id === id)) { const d = (m.defaults || {})[dev]; id = inDev.find(e => e.id === d) ? d : inDev[0].id; }
        this._sel.dev = dev; this._sel.model = id; this._buildPanel();
    }
    // every model of the catalog, installed or not (falls back to the daemon's installed-only list when the model tool is missing)
    _entries() {
        if (this._cat) return this._cat;
        const m = this._models; return ((m && m.models) || []).map(e => Object.assign({}, e, {state: 'installed'}));
    }
    _toolPath(name) { const cli = this._models && this._models.cli; return cli ? GLib.path_get_dirname(cli) + '/' + name : null; }
    _statusDir() { const cli = this._models && this._models.cli; return cli ? GLib.path_get_dirname(GLib.path_get_dirname(cli)) + '/models/.status' : null; }
    _spawnTool(args) {   // detached helper process (own session): downloads never run inside gnome-shell and survive it
        const tool = this._toolPath('voice-typing-models'); if (!tool) return false;
        const setsid = GLib.find_program_in_path('setsid');
        try { Gio.Subprocess.new(setsid ? [setsid, '-f', tool, ...args] : [tool, ...args], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE); return true; }
        catch (e) { console.error('voice-typing models: ' + e); return false; }
    }
    _refreshCat() {
        const tool = this._toolPath('voice-typing-models'); if (!tool || !GLib.file_test(tool, GLib.FileTest.EXISTS)) return;
        let p; try { p = Gio.Subprocess.new([tool, 'status', '--json'], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE); } catch (e) { return; }
        p.communicate_utf8_async(null, null, (pr, res) => {
            try {
                if (!this._bar) return;
                const [, out] = pr.communicate_utf8_finish(res);
                const d = JSON.parse(out); const now = GLib.get_monotonic_time();
                for (const e of d.models) {                                  // a download that was just started may not have written its status file yet
                    const t = this._pend[e.id];
                    if (t && now - t < 6e6 && (e.state === 'missing' || e.state === 'failed' || e.state === 'unverified')) { e.state = 'downloading'; e.pct = 0; e.got = 0; e.total = e.size; e.msg = ''; }
                }
                this._cat = d.models; this._free = d.free;
                const act = d.models.some(e => ['downloading', 'verifying', 'installing', 'building'].includes(e.state));
                if (act) this._dlTickStart();
                if (d.models.some(e => e.state === 'unverified')) {          // downloaded by an older version / by hand: check once, then it counts as installed
                    if (!this._adopted) { this._adopted = true; this._spawnTool(['adopt']); }
                    if (!this._catTimer) this._catTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => { this._catTimer = 0; if (this._bar && this._panelOpen) this._refreshCat(); return GLib.SOURCE_REMOVE; });
                }
                const sig = JSON.stringify(d.models.map(e => [e.id, e.state, e.msg || ''])) + this._confirm + JSON.stringify(this._note);
                if (this._panelOpen && sig !== this._catSig) this._buildPanel();
            } catch (e) { console.error('voice-typing models status: ' + e); }
        });
    }
    // ---------- missing system packages (first launch) ----------
    _refreshDeps(autoOpen) {
        const tool = this._toolPath('voice-typing-models'); if (!tool || !GLib.file_test(tool, GLib.FileTest.EXISTS)) return;
        let p; try { p = Gio.Subprocess.new([tool, 'deps', '--json'], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE); } catch (e) { return; }
        p.communicate_utf8_async(null, null, (pr, res) => {
            try {
                if (!this._bar) return;
                const [, out] = pr.communicate_utf8_finish(res);
                const d = JSON.parse(out); const prev = JSON.stringify(this._deps); this._deps = d;
                const job = d.job && d.job.status;
                if (['installing', 'building'].includes(job)) this._depsTickStart();
                // first launch: open Settings once so the prompt is seen; "Not now" stores the dismissal until the missing list changes
                if (autoOpen && d.needed && !d.dismissed && !this._panelOpen && this._models) { this._depsOpen = true; this._openPanel(); }
                else if (this._panelOpen && prev !== JSON.stringify(d)) this._buildPanel();
            } catch (e) { console.error('voice-typing deps: ' + e); }
        });
    }
    _depsJob() {
        const dir = this._statusDir(); if (!dir) return null;
        try { const [ok, b] = GLib.file_get_contents(`${dir}/_deps.json`); if (ok) return JSON.parse(new TextDecoder().decode(b)); } catch (x) {}
        return null;
    }
    _depsTickStart() { if (!this._depsTimer) this._depsTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => this._depsTick()); }
    _depsTick() {
        if (!this._bar || !this._deps) { this._depsTimer = 0; return GLib.SOURCE_REMOVE; }
        const s = this._depsJob(), old = this._deps.job;
        if (!s && this._depsPend && GLib.get_monotonic_time() - this._depsPend < 6e6) return GLib.SOURCE_CONTINUE;   // just started, no status file yet
        this._depsPend = 0;
        const sig = (j) => j ? j.status + '|' + (j.msg || '') : '';
        if (sig(s) !== sig(old)) { this._deps.job = s; if (this._panelOpen) this._buildPanel(); }
        if (s && ['installing', 'building', 'downloading', 'verifying'].includes(s.status)) return GLib.SOURCE_CONTINUE;
        this._depsTimer = 0;
        this._refreshDeps(false); this._refreshCat();
        if (s && s.status === 'done' && this._panelOpen) { this._sel.dev = 'gpu'; const g = (this._cat || []).find(e => e.id === 'parakeet-gpu' && e.state === 'installed'); if (g) this._sel.model = g.id; }
        return GLib.SOURCE_REMOVE;
    }
    _depsInstall() {
        if (!this._deps) return;
        if (!this._spawnTool(['deps', 'install'])) { this._deps.job = {status: 'failed', msg: 'Could not start the installer.'}; this._buildPanel(); return; }
        this._depsPend = GLib.get_monotonic_time(); this._deps.job = {status: 'installing', msg: 'Waiting for your password…'}; this._buildPanel(); this._depsTickStart();
    }
    _depsNotNow() {
        this._spawnTool(['deps', 'dismiss']);
        if (this._deps) { this._deps.dismissed = true; this._deps.job = null; }
        this._depsOpen = false; this._buildPanel();
    }
    // the prompt (full card when it matters, one compact line after "Not now" so it can be found again in Settings)
    _depsCard() {
        const d = this._deps; if (!d) return null;
        const job = d.job && d.job.status, busy = ['installing', 'building', 'downloading', 'verifying'].includes(job);
        if (!d.needed && !busy && job !== 'failed' && job !== 'done') return null;
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true,
            style: 'margin-top: 8px; padding: 9px 11px; spacing: 6px; border-radius: 8px; border: 1px solid rgba(255,184,77,0.40); background-color: rgba(255,184,77,0.08);'});
        const note = (t, c, sz) => wrapLbl(lbl(t, sz || 9.5, c || 'rgba(255,255,255,0.85)'));
        const row = () => new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
        if (job === 'done') {
            box.style = box.style.replace(/255,184,77/g, '108,203,95');
            box.add_child(note(d.job.msg || 'All set.', 'rgba(108,203,95,0.95)', 10));
            const r = row(); r.add_child(this._pill('OK', 'plain', () => { this._spawnTool(['deps', 'undismiss']); this._deps.job = null; this._buildPanel(); })); box.add_child(r);
            return box;
        }
        if (d.dismissed && !busy && job !== 'failed') {     // compact reminder
            const n = d.total ?? d.missing.length;
            const r = row(); const t = note(`Setup: ${n} optional package${n === 1 ? '' : 's'} missing`, SEC); t.x_expand = true; t.y_align = Clutter.ActorAlign.CENTER;
            r.add_child(t); r.add_child(this._pill('Set up', 'primary', () => { this._deps.dismissed = false; this._spawnTool(['deps', 'undismiss']); this._buildPanel(); }));
            box.style = 'margin-top: 6px; padding: 2px 2px;'; box.add_child(r); return box;
        }
        const gpuMissing = d.missing_gpu.length > 0, typMissing = d.missing_typing.length > 0;
        const noiseMissing = (d.missing_noise || []).length > 0, pipMissing = (d.missing_pip || []).length > 0;
        box.add_child(lbl(gpuMissing || (!d.engine_built && d.gpu !== false) ? 'Graphics-card setup needed' : 'Some packages are missing', 11, '#ffb84d', 'bold'));
        if (gpuMissing) box.add_child(note('The graphics-card engine needs these system packages, and they are not installed: ' + d.missing_gpu.join(', ') + '. ' +
            'The processor (CPU) engine works fine without them, but the graphics-card version can\'t be built or used until they are installed.'));
        else if (!d.engine_built && d.gpu !== false && !d.missing.length) box.add_child(note('All packages are here; the graphics-card engine just has to be built (a few minutes). The processor engine keeps working meanwhile.'));
        if (typMissing) box.add_child(note('Typing: ' + d.missing_typing.join(', ') + ' is not installed. It types the words into your window; without it nothing gets typed.'));
        if (d.missing_perm) box.add_child(note('Typing permission: a one-time system rule that lets the app use the virtual keyboard (/dev/uinput). Without it nothing gets typed.'));
        if (noiseMissing) box.add_child(note('Noise suppression: ' + d.missing_noise.join(', ') + ' is not installed. Dictation works without it, but background noise is not filtered out.'));
        if (pipMissing) box.add_child(note('Better voice detection: ' + d.missing_pip.join(', ') + ' (Python packages for the app, no password needed). Without them a simpler loudness-based detector is used.'));
        if (busy) {
            const r = row(); const sp = this._mkSpinner(14); r.add_child(sp); if (!this._spinId) this._spinId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => this._spinTick());
            const t = note(d.job.msg || 'Working…', '#fff'); t.x_expand = true; t.y_align = Clutter.ActorAlign.CENTER; r.add_child(t); box.add_child(r);
            return box;
        }
        if (job === 'failed') box.add_child(note(d.job.msg || 'It did not work.', '#ff9a9c'));
        if (d.missing.length && !d.pkexec) {
            box.add_child(note('Run this in a terminal to install them:', SEC));
            const c = note(d.manual, '#fff'); c.clutter_text.selectable = true; box.add_child(c);
        }
        const r = row();
        if (d.pkexec || !d.missing.length) r.add_child(this._pill(job === 'failed' ? 'Try again' : 'Install', 'primary', () => this._depsInstall()));
        r.add_child(this._pill('Not now', 'plain', () => this._depsNotNow()));
        box.add_child(r);
        if (d.missing.length && d.pkexec) box.add_child(note('Install asks for your password once' + (d.missing_perm ? ', installs ' + d.missing.join(' ') + ' and the typing rule.' : ', then runs: ' + d.command_text.replace(/^pkexec /, '')), 'rgba(255,255,255,0.55)', 8.5));
        else if (d.missing_perm && d.pkexec) box.add_child(note('Install asks for your password once, then adds the typing rule.', 'rgba(255,255,255,0.55)', 8.5));
        if (pipMissing) box.add_child(note('and downloads into the app: pip install ' + d.missing_pip.join(' '), 'rgba(255,255,255,0.55)', 8.5));
        return box;
    }
    _dlTickStart() { if (!this._dlTimer) this._dlTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => this._dlTick()); }
    _dlTick() {
        const dir = this._statusDir();
        if (!this._bar || !dir || !this._cat) { this._dlTimer = 0; return GLib.SOURCE_REMOVE; }
        const ACT = ['downloading', 'verifying', 'installing', 'building']; const now = GLib.get_monotonic_time();
        let any = false, changed = false, refresh = false;
        for (const e of this._cat) {
            if (!ACT.includes(e.state) && !this._pend[e.id]) continue;
            any = true; let s = null;
            try { const [ok, b] = GLib.file_get_contents(`${dir}/${e.id}.json`); if (ok) s = JSON.parse(new TextDecoder().decode(b)); } catch (x) {}
            if (!s) {   // no status file: finished (installed / cancelled) or never started
                if (this._pend[e.id] && now - this._pend[e.id] < 6e6) continue;
                delete this._pend[e.id]; refresh = true; continue;
            }
            delete this._pend[e.id];
            if (s.status !== e.state) { e.state = s.status; changed = true; if (s.status === 'failed') refresh = true; }
            e.pct = s.pct || 0; e.got = s.got || 0; e.total = s.total || e.size; e.msg = s.msg || '';
        }
        if (changed) this._buildPanel(); else if (this._panelOpen) for (const e of this._cat) if (this._dlW[e.id]) this._dlApply(e);
        if (refresh) this._refreshCat();
        if (!any && !refresh) { this._dlTimer = 0; return GLib.SOURCE_REMOVE; }
        return GLib.SOURCE_CONTINUE;
    }
    _mb(n) { return n >= 1073741824 ? (n / 1073741824).toFixed(1) + ' GB' : n >= 1048576 ? Math.round(n / 1048576) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
    _dlApply(e) {   // in-place progress update (no panel rebuild, so scrolling and hover are undisturbed)
        const w = this._dlW[e.id]; if (!w) return;
        const pct = Math.max(0, Math.min(100, e.pct || 0));
        if (e.state === 'downloading') {
            w.fill.width = Math.round(w.trackW * pct / 100); w.fill.style = `border-radius: 3px; background-color: ${ACCENT};`;
            w.label.text = e.msg && e.msg !== '' ? e.msg : `${pct}%  ·  ${this._mb(e.got || 0)} of ${this._mb(e.total || e.size)}`;
        } else {
            w.fill.width = e.state === 'verifying' ? Math.round(w.trackW * pct / 100) : w.trackW; w.fill.style = `border-radius: 3px; background-color: ${e.state === 'verifying' ? ACCENT : 'rgba(76,194,255,0.45)'};`;
            w.label.text = e.state === 'verifying' ? `Checking the download…  ${pct}%` : (e.msg || (e.state === 'building' ? 'Building the graphics-card engine…' : 'Installing…'));
        }
    }
    _doDownload(id) {
        const e = (this._cat || []).find(x => x.id === id); if (!e) return;
        delete this._note[id]; this._confirm = '';
        if (!this._spawnTool(['install', id])) { this._note[id] = 'Could not start the download.'; this._buildPanel(); return; }
        e.state = 'downloading'; e.pct = 0; e.got = 0; e.total = e.size; e.msg = ''; this._pend[id] = GLib.get_monotonic_time();
        this._buildPanel(); this._dlTickStart();
    }
    _doCancel(id) {
        const tool = this._toolPath('voice-typing-models'); if (!tool) return;
        const e = (this._cat || []).find(x => x.id === id); if (e) { e.msg = 'Cancelling…'; this._dlApply(e); }
        delete this._pend[id];
        try { const p = Gio.Subprocess.new([tool, 'cancel', id], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
              p.wait_async(null, () => { if (this._bar) this._refreshCat(); }); } catch (x) {}
    }
    _doRemove(id) {
        const tool = this._toolPath('voice-typing-models'); if (!tool) return;
        this._confirm = ''; delete this._note[id];
        let p; try { p = Gio.Subprocess.new([tool, 'remove', id, '--active', (this._models && this._models.current) || ''], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE); } catch (x) { return; }
        if (this._sel.model === id) this._sel.model = this._models.current;
        p.communicate_utf8_async(null, null, (pr, res) => {
            try {
                const [, out, err] = pr.communicate_utf8_finish(res);
                if (!pr.get_successful()) this._note[id] = ((err || out || '').trim().split('\n')[0]) || 'Could not uninstall it.';
            } catch (x) { this._note[id] = 'Could not uninstall it.'; }
            if (this._bar) { this._catSig = ''; this._refreshCat(); }
        });
    }
    _dirty() { const a = this._sel, b = this._base; return a.model !== b.model || a.pause !== b.pause || a.mic !== b.mic || a.sounds !== b.sounds; }

    // ---------- position (drag) ----------
    // The dragged position lives only in this._dragPos for the current open session (cleared by _hide); nothing is saved or reloaded.
    _log(kind, extra) {   // placement debug log, written ONLY if the file $XDG_RUNTIME_DIR/gnome-voice-typing-overlay/debug exists (off by default): ~/.cache/gnome-voice-typing/overlay.log
        try {
            if (!GLib.file_test(DIR + '/debug', GLib.FileTest.EXISTS)) return;
            const f = GLib.get_user_cache_dir() + '/gnome-voice-typing/overlay.log';
            GLib.mkdir_with_parents(GLib.path_get_dirname(f), 0o755);
            try { if (GLib.file_test(f, GLib.FileTest.EXISTS) && Gio.File.new_for_path(f).query_info('standard::size', 0, null).get_size() > 300000) GLib.file_set_contents(f, ''); } catch (e) {}
            const o = Gio.File.new_for_path(f).append_to(Gio.FileCreateFlags.NONE, null);
            o.write_all(new TextEncoder().encode(`${new Date().toISOString()} ${kind} ${extra || ''}\n`), null); o.close(null);
        } catch (e) {}
    }
    _area(x, y) {   // work area of the monitor containing (x, y), else primary
        const L = Main.layoutManager;
        let idx = L.primaryIndex;
        for (let i = 0; i < L.monitors.length; i++) {
            const m = L.monitors[i];
            if (x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height) { idx = i; break; }
        }
        return L.getWorkAreaForMonitor(idx);
    }
    _clamp(x, y, w, h) {
        const a = this._area(x + w / 2, y + h / 2);
        return [Math.round(Math.min(Math.max(x, a.x + MARGIN), a.x + a.width - w - MARGIN)),
                Math.round(Math.min(Math.max(y, a.y + MARGIN), a.y + a.height - h - MARGIN))];
    }
    _barSize() { return [BAR_W, this._bar.get_preferred_height(-1)[1]]; }

    _focusWin() {
        const w = global.display.focus_window; if (!w) return null;
        try { const t = w.get_window_type(); if (t === Meta.WindowType.DESKTOP || t === Meta.WindowType.DOCK) return null; if (w.minimized) return null; } catch (e) {}
        return w;
    }
    _plan(f) {   // where the bar goes: text field/caret, else lower-centre of the focused window, else bottom-centre of the primary monitor
        const [bw, bh] = this._barSize();
        if (f && !f.caret && f.field[2] >= 800 && f.field[3] >= 500) f = null;   // huge caret-less 'field' = a document canvas: use the window-relative plan instead of hugging its top edge
        if (f) { const [x, y, slide] = this._nearField(f); return {x, y, slide, src: f.src || 'near-field', rect: f.field, key: f.key}; }
        const w = this._focusWin();
        if (w) {
            const fr = w.get_frame_rect(); let L = fr.x, T = fr.y, R = fr.x + fr.width, B = fr.y + fr.height;
            try { const mi = w.get_monitor(); if (mi >= 0) { const a = Main.layoutManager.getWorkAreaForMonitor(mi); L = Math.max(L, a.x); T = Math.max(T, a.y); R = Math.min(R, a.x + a.width); B = Math.min(B, a.y + a.height); } } catch (e) {}
            if (R - L >= bw && B - T >= bh * 2) {
                const [x, y] = this._clamp(L + (R - L - bw) / 2, B - bh - 48, bw, bh);
                return {x, y, slide: -6, src: 'window', rect: [fr.x, fr.y, fr.width, fr.height], key: 'wf:' + w.get_id()};
            }
        }
        const mon = Main.layoutManager.primaryMonitor; if (!mon) return {x: 0, y: 0, slide: 6, src: 'default', rect: null, key: 'default'};
        const [x, y] = this._clamp(mon.x + (mon.width - bw) / 2, mon.y + mon.height - bh - 96, bw, bh);
        return {x, y, slide: 6, src: 'default', rect: null, key: 'default'};
    }
    _moveTo(x, y, anim) {
        const [w, h] = this._barSize();
        [x, y] = this._clamp(x, y, w, h);
        this._target = [x, y];
        this._bar.remove_transition('x'); this._bar.remove_transition('y');
        if (anim && this._bar.visible) this._bar.ease({x, y, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
        else this._bar.set_position(x, y);
        this._placePanel(); if (anim) GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => { if (this._bar) this._writeGeom(); return GLib.SOURCE_REMOVE; });
    }
    _placePanel() {
        if (!this._panelOpen || !this._panel.visible) { this._writeGeom(); return; }
        const [bx, by] = this._target || this._bar.get_position(), [bw, bh] = this._barSize();
        const pw = PANEL_W, ph = this._panel.get_preferred_height(pw)[1];
        const a = this._area(bx + bw / 2, by + bh / 2);
        const top = a.y + MARGIN, bottom = a.y + a.height - MARGIN;
        let px = Math.min(Math.max(bx + bw / 2 - pw / 2, a.x + MARGIN), a.x + a.width - pw - MARGIN), py;
        if (by - 10 - ph >= top) py = by - 10 - ph;                        // above the bar
        else if (by + bh + 10 + ph <= bottom) py = by + bh + 10;            // below the bar
        else {                                                             // short screen: beside the bar, never covering it
            px = (bx + bw + 10 + pw <= a.x + a.width - MARGIN) ? bx + bw + 10 : Math.max(a.x + MARGIN, bx - 10 - pw);
            py = Math.min(Math.max(by + bh / 2 - ph / 2, top), bottom - ph);
        }
        this._panel.set_position(Math.round(px), Math.round(py));
        this._writeGeom();
    }

    // ---------- near the focused text field (AT-SPI probe in bin/focusbox.py) ----------
    _focusboxPath() {
        const cli = this._models && this._models.cli; if (!cli) return null;
        return GLib.path_get_dirname(cli) + '/focusbox.py';
    }
    _locate(budget, cb) {   // async: never blocks the shell; cb(null) when nothing is known
        if (this._caret && typeof this._caret.getFocusRect === 'function') {
            let r = null;
            try { r = this._caret.getFocusRect(); } catch (e) { r = null; }
            Promise.resolve(r).then(v => {
                const f = this._fromRect(v); this._dbgLoc = {caretjs: v ? JSON.stringify(v) : 'null', mapped: !!f};
                if (f) cb(f); else this._locateProbe(budget, cb);
            }).catch(() => this._locateProbe(budget, cb));
            return;
        }
        this._dbgLoc = {caretjs: 'unavailable'};
        this._locateProbe(budget, cb);
    }
    _fromRect(r) {   // caret.js: one rect in "screen" coords that may really be window-relative on Wayland -> sanity-check against the focused window
        if (!r || !Number.isFinite(r.x) || !Number.isFinite(r.y) || !(r.h > 0)) return null;
        const win = global.display.focus_window; if (!win) return null;
        const fr = win.get_frame_rect(), br = win.get_buffer_rect();
        const inside = (x, y) => x >= fr.x - 4 && x <= fr.x + fr.width + 4 && y >= fr.y - 4 && y <= fr.y + fr.height + 4;
        let x = r.x, y = r.y;
        if (!inside(x, y)) { x += br.x; y += br.y; if (!inside(x, y)) return null; }
        const w = Math.max(r.w || 0, 1), isCaret = r.kind ? r.kind === 'caret' : !(r.w > 6);
        return {key: 'win:' + win.get_id() + (r.fid ? ':' + r.fid : ''), src: isCaret ? 'caret' : 'near-field', field: [x, y, w, r.h], caret: isCaret ? [x, y, r.h] : null};
    }
    _locateProbe(budget, cb) {
        const path = this._focusboxPath();
        if (!path || !GLib.file_test(path, GLib.FileTest.EXISTS)) { cb(null); return; }
        let proc;
        try { proc = Gio.Subprocess.new(['/usr/bin/python3', path, String(budget)], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE); }
        catch (e) { cb(null); return; }
        let killId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.round(budget * 1000) + 900, () => { killId = 0; try { proc.force_exit(); } catch (e) {} return GLib.SOURCE_REMOVE; });
        proc.communicate_utf8_async(null, null, (p, res) => {
            if (killId) { GLib.source_remove(killId); killId = 0; }
            let out = null;
            try { out = JSON.parse(p.communicate_utf8_finish(res)[1]); } catch (e) {}
            const sc = this._toScreen(out); if (this._dbgLoc) this._dbgLoc.focusbox = out && out.field ? JSON.stringify(out) : 'null'; cb(sc);
        });
    }
    _toScreen(o) {   // window-relative AT-SPI extents -> screen coords using the focused window's surface origin
        if (!this._bar || !o || !o.field || !o.key) return null;
        let ox = 0, oy = 0;
        if (o.coord !== 'screen') {
            const w = global.display.focus_window; if (!w) return null;
            const r = w.get_buffer_rect(); ox = r.x; oy = r.y;
        }
        const f = o.field, c = o.caret;
        return {key: o.key, src: 'focusbox', field: [f[0] + ox, f[1] + oy, f[2], f[3]], caret: c ? [c[0] + ox, c[1] + oy, c[2]] : null};
    }
    _nearField(f) {   // returns [x, y, slide]: below the field if it fits, else above; big text areas use the caret line instead of the whole box
        const [bw, bh] = this._barSize(), GAP = 8;
        const F = f.field, C = f.caret;
        const a = this._area(F[0] + F[2] / 2, F[1] + F[3] / 2);
        const big = !!C && F[3] > 140;
        const top = big ? C[1] : F[1], bottom = big ? C[1] + C[2] : F[1] + F[3];
        const roomBelow = a.y + a.height - MARGIN - (bottom + GAP), roomAbove = top - GAP - (a.y + MARGIN);
        let y, slide;
        if (roomBelow >= bh) { y = bottom + GAP; slide = 6; }
        else if (roomAbove >= bh) { y = top - GAP - bh; slide = -6; }
        else if (roomBelow >= roomAbove) { y = bottom + GAP; slide = 6; }   // cramped: take the roomier side; clamp keeps it on-screen
        else { y = top - GAP - bh; slide = -6; }
        let x = (C ? C[0] : F[0]) - 24;                                    // slightly left of the caret
        [x, y] = this._clamp(x, y, bw, bh);
        return [x, y, slide];
    }
    _startPlacement() {
        if (this._locating) return;
        this._locating = true; const tok = ++this._locTok;
        this._writeFocus();
        try { const fw = this._focusWin(); if (fw && this._caret && this._caret.isReady() && this._caret.seedFocus) this._caret.seedFocus(fw.get_pid()); } catch (err) {}   // field already focused before the bar appeared: no focus event exists for it
        this._locate(0.35, (f) => {
            this._locating = false;
            if (!this._bar || tok !== this._locTok || this._state === 'hidden') return;
            const wasMoved = this._userMoved, hadDrag = !!this._dragPos;
            const P = this._plan(f), x = P.x, y = P.y, slide = P.slide;
            this._field = f; this._curKey = P.key; this._dragPos = null;
            this._log('show', `src=${P.src} rect=${JSON.stringify(P.rect)} pos=${x},${y} userMovedBefore=${wasMoved} staleDragPos=${hadDrag} ${JSON.stringify(this._dbgLoc || {})}`);
            this._userMoved = false; this._placed = true;
            this._bar.remove_all_transitions(); this._bar.visible = true; this._bar.opacity = 0;
            this._moveTo(x, y, false);
            this._bar.translation_y = slide;
            this._bar.ease({opacity: 255, translation_y: 0, duration: 170, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
            this._startFollow(); this._geomSoon();
        });
    }
    // while visible: re-probe on window-focus change and every few seconds; move ONLY if the focused field is a different one
    _startFollow() {
        if (this._followId) return;
        this._followId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2500, () => {
            // caret.js does sync D-Bus: never poll it; with it, only a window-focus change re-probes. The subprocess probe may poll.
            if (!(this._caret && typeof this._caret.getFocusRect === 'function')) this._follow();
            return GLib.SOURCE_CONTINUE;
        });
        this._focusSig = global.display.connect('notify::focus-window', () => {
            this._writeFocus();
            if (this._fsTimer) GLib.source_remove(this._fsTimer);
            this._fsTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 180, () => { this._fsTimer = 0; this._follow(); return GLib.SOURCE_REMOVE; });
        });
    }
    _writeFocus() {   // which app has focus -> $XDG_RUNTIME_DIR/gnome-voice-typing-overlay/focus.json, so bin/dictated.py can paste with Ctrl+Shift+V in terminals
        try {
            const w = global.display.focus_window;
            const o = w ? {wm_class: w.get_wm_class() || '', app_id: w.get_gtk_application_id() || '', sandbox_id: w.get_sandboxed_app_id() || '', pid: w.get_pid(), t: Math.round(GLib.get_real_time() / 1000)} : {};
            GLib.file_set_contents(DIR + '/focus.json', JSON.stringify(o));
        } catch (err) {}
    }
    _focusPoke() {   // caret.js says focus moved to another accessible: re-probe shortly (debounced), so the bar follows field changes inside one window
        if (!this._bar || this._state === 'hidden' || !this._placed) return;
        if (this._fsTimer) GLib.source_remove(this._fsTimer);
        this._fsTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => { this._fsTimer = 0; this._follow(); return GLib.SOURCE_REMOVE; });
    }
    _stopFollow() {
        if (this._followId) { GLib.source_remove(this._followId); this._followId = 0; }
        if (this._fsTimer) { GLib.source_remove(this._fsTimer); this._fsTimer = 0; }
        if (this._focusSig) { global.display.disconnect(this._focusSig); this._focusSig = 0; }
    }
    _follow() {
        if (!this._bar || this._state === 'hidden' || !this._placed || this._locating || this._following || this._dragging) return;
        this._following = true;
        this._locate(0.25, (f) => {
            this._following = false;
            if (!this._bar || this._state === 'hidden') return;
            if (this._userMoved) return;                                    // dragged: the bar stays where the user put it until dictation closes
            const P = this._plan(f);
            if (P.key === this._curKey) return;                             // same field / same window (the user is typing): never move
            this._field = f; this._curKey = P.key;
            this._log('move', `src=${P.src} rect=${JSON.stringify(P.rect)} pos=${P.x},${P.y} userMoved=${this._userMoved}`);
            this._moveTo(P.x, P.y, true);
        });
    }

    _dragBegin(ev) {
        if (ev.get_button() !== 1 || this._dragging) return Clutter.EVENT_PROPAGATE;
        const [px, py] = ev.get_coords(), [bx, by] = this._bar.get_position();
        this._dragOff = [px - bx, py - by]; this._dragging = true; this._umPrev = this._userMoved; this._dragMoved = false; this._userMoved = true;
        this._bar.remove_transition('x'); this._bar.remove_transition('y');
        this._bar.style = this._barStyle(true);
        try { global.display.set_cursor(Meta.Cursor.GRABBING); } catch (e) {}
        // a real stage grab keeps every pointer event coming to the bar even when the pointer outruns it and lands over a client window
        try { this._grab = global.stage.grab(this._bar); } catch (e) { this._grab = null; }
        // one handler, fed both by the stage (capture phase) and by the bar itself (receives everything while the stage grab is held)
        const onEv = (e) => {
            if (!this._dragging) return Clutter.EVENT_PROPAGATE;
            const t = e.type();
            if (t === Clutter.EventType.MOTION) {
                if (!(e.get_state() & Clutter.ModifierType.BUTTON1_MASK)) { this._dragEnd(true); return Clutter.EVENT_PROPAGATE; }
                const [mx, my] = e.get_coords(), [w, h] = this._barSize();
                const [x, y] = this._clamp(mx - this._dragOff[0], my - this._dragOff[1], w, h);
                if (!this._target || this._target[0] !== x || this._target[1] !== y) {
                    this._dragMoved = true; this._bar.set_position(x, y); this._target = [x, y]; this._dragPos = {x, y}; this._panelSoon();   // cheap: set_position only; panel follows on idle
                }
                return Clutter.EVENT_STOP;
            }
            if (t === Clutter.EventType.BUTTON_RELEASE) { this._dragEnd(true); return Clutter.EVENT_STOP; }
            return Clutter.EVENT_PROPAGATE;
        };
        this._capId = global.stage.connect('captured-event', (s, e) => onEv(e));
        this._barEvIds = [this._bar.connect('motion-event', (a, e) => onEv(e)), this._bar.connect('button-release-event', (a, e) => onEv(e))];
        return Clutter.EVENT_STOP;
    }
    _panelSoon() {   // coalesced: at most one panel re-place per idle, none if the panel is closed
        if (!this._panelOpen || this._panelIdle) return;
        this._panelIdle = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => { this._panelIdle = 0; if (this._bar) this._placePanel(); return GLib.SOURCE_REMOVE; });
    }
    _dragEnd(save) {
        if (this._capId) { global.stage.disconnect(this._capId); this._capId = 0; }
        if (this._grab) { try { this._grab.dismiss(); } catch (e) {} this._grab = null; }
        if (this._barEvIds && this._bar) { for (const id of this._barEvIds) this._bar.disconnect(id); }
        this._barEvIds = null;
        if (this._panelIdle) { GLib.source_remove(this._panelIdle); this._panelIdle = 0; }
        if (!this._dragging) return;
        this._dragging = false;
        if (!this._dragMoved) this._userMoved = !!this._umPrev;   // a plain click on the bar is not a drag: keep following the caret
        try { global.display.set_cursor(Meta.Cursor.DEFAULT); } catch (e) {}
        if (this._bar) this._bar.style = this._barStyle(this._bar.hover);
        if (this._dragPos) this._log('drag-end', `pos=${this._dragPos.x},${this._dragPos.y} (session only, not saved)`);
        if (this._bar) { this._placePanel(); this._geomSoon(); }
    }

    _writeGeom() {   // coordinates of clickable bits (used by the automated test; harmless otherwise)
        if (this._dragging) return;   // never do file I/O per pointer event while dragging
        try {
            const c = (a) => { const [x, y] = a.get_transformed_position(); return [Math.round(x + a.width / 2), Math.round(y + a.height / 2)]; };
            const g = {target: this._target, barVisible: !!(this._bar.visible && this._bar.opacity > 0), mic_btn: c(this._mic), bar: c(this._bar), grip: c(this._grip), gear: c(this._gear), close: c(this._close), rows: {}, chips: {}, saveState: this._saveState};
            if (this._panelOpen) {
                for (const [k, a] of Object.entries(this._rowActors || {})) g.rows[k] = c(a);
                for (const [k, a] of Object.entries(this._chipActors || {})) g.chips[k] = c(a);
                if (this._saveBtn) g.save = c(this._saveBtn);
                if (this._micRow) g.mic = c(this._micRow);
                if (this._soundRow) g.sounds = c(this._soundRow);
                g.dev = {}; for (const [k, a] of Object.entries(this._devActors || {})) g.dev[k] = c(a);
                if (this._panelClose) g.panelClose = c(this._panelClose);
                if (this._panelBack) g.back = c(this._panelBack);
                g.page = this._page || 'home'; g.cats = {}; for (const [k, a] of Object.entries(this._catActors || {})) g.cats[k] = c(a);
            }
            const key = JSON.stringify(g);
            if (key !== this._geomKey) { this._geomKey = key; GLib.file_set_contents(DIR + '/geometry.json', key); }
        } catch (e) {}
    }
    _geomSoon() { GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => { if (this._bar) this._writeGeom(); return GLib.SOURCE_REMOVE; }); }

    // ---------- bar ----------
    _setMic(bg, icon, iconColor, ring, dim) {
        this._micS = {bg, icon, fg: iconColor, ring, dim};
        this._micIcon.icon_name = icon;
        if (this._mic._upd) this._mic._upd(); else this._micStyle({hover: false, down: false});
    }
    _micStyle(st) {
        const S = this._micS, HOV = {'#4cc2ff': '#7ad4ff', [IDLE_BG]: 'rgba(255,255,255,0.17)', '#ffd166': '#ffdc8a', '#e5484d': '#ee6a6e'},
              DWN = {'#4cc2ff': '#33a5dd', [IDLE_BG]: 'rgba(255,255,255,0.05)', '#ffd166': '#e6b94f', '#e5484d': '#c93a3f'};
        const bg = st.down ? (DWN[S.bg] || S.bg) : st.hover ? (HOV[S.bg] || S.bg) : S.bg;
        const op = S.dim ? 110 : 255;
        this._mic.style = `background-color: ${bg}; border-radius: 12px; ${S.ring > 0 ? `box-shadow: 0 0 0 ${S.ring}px ${ACCENT_DIM};` : ''} opacity: ${op};`;   // no shadow when idle: a translucent circle would show it through
        this._micIcon.style = `color: ${S.fg};`;
        this._mic.scale_x = this._mic.scale_y = st.down ? 0.92 : 1.0;
    }
    _modelStyle(color, bold, pt = 8) { return `${FONT} font-size: ${pt}pt; color: ${color};${bold ? ' font-weight: bold;' : ''}`; }

    _apply(state, level, text, busy) {
        if (!this._bar) return;
        if (state === 'hidden' || state === 'suspended') { this._chimeFor(state); this._closePanel(); this._hide(); return; }   // suspended: the bar shows nothing
        this._chimeFor(state);
        this._state = state;
        const i = this._info, loading = !!i.loading;
        const dv = i.device ? ' · ' + i.device : '';
        if (loading) { this._modelLabel.text = `Loading ${shortName(i.loading)}…`; this._modelLabel.style = this._modelStyle(ACCENT); }
        // 'Now <model>' at 7pt fits the 204 px bar (the long 'Switched to ...' was cut off); ellipsize END still covers long names
        else if (i.switched) { this._modelLabel.text = `Now ${shortName(i.switched)}${dv}`; this._modelLabel.style = this._modelStyle(ACCENT, true, 7); }
        else { this._modelLabel.text = i.model ? shortName(i.model) + dv : ''; this._modelLabel.style = this._modelStyle('rgba(255,255,255,0.55)'); }
        this._barSpin.visible = loading; this._ensureSpin();
        if (state === 'listening') {
            this._label.text = text || 'Listening…';
            this._setMic(busy ? '#ffd166' : ACCENT, 'audio-input-microphone-symbolic', '#000', Math.round(level * 5), loading);
        } else if (state === 'starting') {
            this._label.text = 'Getting ready…'; this._setMic(IDLE_BG, 'audio-input-microphone-symbolic', '#e8e8e8', 0, loading);
        } else if (state === 'paused') {
            this._label.text = text || 'Paused'; this._setMic(IDLE_BG, 'microphone-disabled-symbolic', '#e8e8e8', 0, loading);
        } else {
            this._label.text = text || 'Something went wrong'; this._setMic('#e5484d', 'dialog-warning-symbolic', '#fff', 0, false);
        }
        this._show();
        if (this._panelOpen) {
            if (this._isLoading() !== this._panelLoading) this._buildPanel(); else this._updatePanelStatus();
        }
    }

    // sounds: bin/chime.sh <start|stop|wake> (it honours config 'sounds'); fired on state transitions only
    _chimeFor(state) {
        const prev = this._rawPrev || 'hidden'; this._rawPrev = state;
        if (state === prev) return;
        if (state === 'listening') this._chime(prev === 'hidden' || prev === 'suspended' || prev === 'starting' || prev === 'paused' || prev === 'error' ? 'start' : null);
        else if ((state === 'paused' || state === 'hidden' || state === 'suspended') && prev === 'listening') this._chime('stop');
        if (prev === 'suspended' && state !== 'hidden' && state !== 'suspended') this._chime('wake');
    }
    _chime(kind) {
        if (!kind) return;
        const cli = this._models && this._models.cli;
        const sh = cli ? GLib.path_get_dirname(cli) + '/chime.sh' : null;
        if (!sh || !GLib.file_test(sh, GLib.FileTest.EXISTS)) return;
        try { Gio.Subprocess.new([sh, kind], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE); } catch (e) {}
    }

    _show() {
        if (!this._bar) return;
        if (!this._depsChecked && this._toolPath('voice-typing-models')) { this._depsChecked = true; this._refreshDeps(true); }
        if (!this._placed) { this._startPlacement(); return; }   // bar stays hidden until it knows where to go (<= ~0.4 s)
        this._bar.visible = true;
        if (this._bar.opacity < 255) { this._bar.remove_all_transitions(); this._bar.ease({opacity: 255, duration: 120, mode: Clutter.AnimationMode.EASE_OUT_QUAD}); }
    }
    _hide() {
        if (this._placed || this._state !== 'hidden') this._log('hide', `userMoved=${this._userMoved} dragPos=${JSON.stringify(this._dragPos || null)} -> cleared`);
        this._state = 'hidden'; this._placed = false; this._locTok++; this._locating = false; this._field = null; this._curKey = null; this._dragPos = null; this._userMoved = false; this._following = false; if (this._rawPrev !== 'suspended') this._rawPrev = 'hidden';
        this._stopFollow(); this._dragEnd(true);
        if (!this._bar || !this._bar.visible) return;
        this._bar.remove_all_transitions();
        this._bar.ease({opacity: 0, duration: 160, mode: Clutter.AnimationMode.EASE_IN_QUAD,
                        onComplete: () => { if (this._bar && this._state === 'hidden') { this._bar.visible = false; this._writeGeom(); } }});
    }
    _watchdog() {
        if (!this._bar || this._state === 'hidden') return;
        const age = (GLib.get_monotonic_time() - this._last) / 1e6;
        const alive = GLib.file_test(RT + '/gnome-voice-typing.sock', GLib.FileTest.EXISTS);
        if (!alive || (this._state === 'listening' && age > 4)) { this._closePanel(); this._hide(); }
    }

    // ---------- settings panel ----------
    _togglePanel() { this._panelOpen ? this._closePanel() : this._openPanel(); }
    _openPanel() {
        if (!this._models) return;
        this._syncSel(); this._saveState = 'idle'; this._readyUntil = 0;
        if (this._saveTimer) { GLib.source_remove(this._saveTimer); this._saveTimer = 0; }
        this._confirm = ''; this._note = {}; this._scrollKeep = 0; this._page = this._page === 'setup' ? 'setup' : null; this._unConfirm = false; this._panelOpen = true; this._buildPanel(); this._refreshCat(); if (!this._depsOpen) this._refreshDeps(false); this._depsOpen = false;
        this._panel.visible = true; this._panel.reactive = true; this._placePanel(); this._geomSoon();
        this._panel.remove_all_transitions(); this._panel.ease({opacity: 255, duration: 120, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }
    _closePanel() {
        if (!this._panel || !this._panelOpen) return;
        this._panelOpen = false; this._panel.reactive = false; this._panel.remove_all_transitions();
        // closing resets the Save button: on reopen it starts dimmed and wakes up on the first change
        this._saveState = 'idle';
        if (this._saveTimer) { GLib.source_remove(this._saveTimer); this._saveTimer = 0; }
        this._panel.ease({opacity: 0, duration: 120, onComplete: () => { if (this._panel && !this._panelOpen) this._panel.visible = false; }});
        this._writeGeom();
    }

    _vadj() { const sv = this._listScroll; if (!sv) return null; try { return (sv.vadjustment || (sv.vscroll && sv.vscroll.adjustment)) || null; } catch (e) { return null; } }
    _rememberScroll() { const a = this._vadj(); if (a) this._scrollKeep = a.value; }
    _restoreScroll() {   // a rebuild would jump the model list back to the top
        const keep = this._scrollKeep; if (!keep) return;
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 30, () => { const a = this._vadj(); if (a && this._panelOpen) a.value = Math.min(keep, Math.max(0, a.upper - a.page_size)); return GLib.SOURCE_REMOVE; });
    }
    _isLoading() { return !!(this._info && this._info.loading) || (this._models && this._models.switch && this._models.switch.status === 'loading'); }

    // system-impact tag (Light green / Medium amber / Heavy red) + muted detail; both optional (models.json 'impact', 'impact_detail')
    // small text button for model rows: primary (accent) | danger (red) | plain
    _pill(text, kind, onClick, enabled = true) {
        const C = {primary: ['#4cc2ff', 'rgba(76,194,255,0.14)', 'rgba(76,194,255,0.30)', 'rgba(76,194,255,0.55)'],
                   danger: ['#ff9a9c', 'rgba(255,107,107,0.12)', 'rgba(255,107,107,0.26)', 'rgba(255,107,107,0.55)'],
                   plain: ['#ffffff', 'rgba(255,255,255,0.07)', 'rgba(255,255,255,0.15)', 'rgba(255,255,255,0.30)']}[kind];
        const l = lbl(text, 10, C[0], 'bold'); const b = new St.Bin({child: l, y_align: Clutter.ActorAlign.CENTER, x_align: Clutter.ActorAlign.START});
        return this._interactive(b, (a, s) => {
            const fg = s.enabled ? C[0] : 'rgba(255,255,255,0.55)';
            l.style = `${FONT} font-size: 10pt; font-weight: bold; color: ${fg};`;
            a.style = `padding: 3px 12px; border-radius: 6px; border: 1px solid ${s.enabled ? C[3] : 'rgba(255,255,255,0.14)'}; background-color: ` +
                      (!s.enabled ? 'rgba(255,255,255,0.04)' : s.down ? C[2] : s.hover ? C[2] : C[1]) + ';';
        }, onClick, () => enabled);
    }

    // the line under a model's description: Download · size | progress + Cancel | Retry | Uninstall (+ confirm)
    _actionLine(e, m) {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'padding-top: 6px; spacing: 5px;'});
        const line = () => new St.BoxLayout({x_expand: true, style: 'spacing: 10px;', y_align: Clutter.ActorAlign.CENTER});
        const note = (t, c) => { const n = wrapLbl(lbl(t, 9.5, c || SEC)); n.y_align = Clutter.ActorAlign.CENTER; return n; };
        const st = e.state;
        if (st === 'installed') {
            const isCur = e.id === m.current, loading = m.switch && m.switch.status === 'loading' && m.switch.target === e.id;
            const l = line();
            if (this._confirm === e.id) {
                l.add_child(note('Remove it from this computer?', '#fff'));
                l.add_child(this._pill('Remove', 'danger', () => this._doRemove(e.id)));
                l.add_child(this._pill('Keep', 'plain', () => { this._confirm = ''; this._buildPanel(); }));
            } else {
                l.add_child(this._pill('Uninstall', 'plain', () => { this._confirm = e.id; this._buildPanel(); }, !isCur && !loading));
                l.add_child(note('Installed · ' + (e.size_h || ''), 'rgba(108,203,95,0.95)'));
            }
            box.add_child(l);
            if (this._confirm !== e.id && (isCur || loading))     // the reason gets its own full-width line so it can wrap
                box.add_child(note(isCur ? 'In use right now. Pick another model and press Save, then you can uninstall this one.' : 'It is loading right now.'));
        } else if (['downloading', 'verifying', 'installing', 'building'].includes(st)) {
            const trackW = 300;
            const track = new St.BoxLayout({width: trackW, height: 6, style: 'border-radius: 3px; background-color: rgba(255,255,255,0.14);', x_align: Clutter.ActorAlign.START});
            const fill = new St.Widget({height: 6, width: 0, style: `border-radius: 3px; background-color: ${ACCENT};`});
            track.add_child(fill); box.add_child(track);
            const l = line(); const label = wrapLbl(lbl('', 9.5, 'rgba(255,255,255,0.9)')); label.y_align = Clutter.ActorAlign.CENTER;
            l.add_child(label); l.add_child(this._pill('Cancel', 'plain', () => this._doCancel(e.id))); box.add_child(l);
            this._dlW[e.id] = {fill, label, trackW}; this._dlApply(e);
        } else if (st === 'failed') {
            box.add_child(note(e.msg || 'The download failed.', '#ff9a9c'));
            const l = line(); l.add_child(this._pill('Retry', 'primary', () => this._doDownload(e.id))); box.add_child(l);
        } else if (st === 'unverified') {
            box.add_child(note('Checking the files already on disk…'));
        } else if (st === 'setup') {
            box.add_child(note(e.setup === 'engine' ? 'The files are here; the graphics-card engine still has to be built.' : 'The files are here; the faster-whisper add-on still has to be installed.'));
            const l = line(); l.add_child(this._pill('Set up', 'primary', () => this._doDownload(e.id))); box.add_child(l);
        } else {   // missing
            const l = line(); l.add_child(this._pill('Download · ' + (e.size_h || this._mb(e.size || 0)), 'primary', () => this._doDownload(e.id)));
            box.add_child(l);
        }
        if (this._note[e.id]) box.add_child(note(this._note[e.id], '#ff9a9c'));
        return box;
    }

    _impactRow(e) {
        const impact = (e.impact || '').toString().trim(), detail = (e.impact_detail || '').toString().trim();
        if (!impact && !detail) return null;
        const COL = {light: ['#6ccb5f', 'rgba(108,203,95,0.16)'], medium: ['#ffb84d', 'rgba(255,184,77,0.16)'], heavy: ['#ff6b6b', 'rgba(255,107,107,0.16)']};
        const [fg, bg] = COL[impact.toLowerCase()] || ['rgba(255,255,255,0.85)', 'rgba(255,255,255,0.12)'];
        const row = new St.BoxLayout({x_expand: true, style: 'spacing: 8px; padding-top: 3px;'});
        if (impact) {
            const tag = new St.Bin({y_align: Clutter.ActorAlign.START, style: `padding: 1px 8px; border-radius: 8px; background-color: ${bg};`,
                                    child: lbl(impact, 9, fg, 'bold')});
            row.add_child(tag);
        }
        if (detail) { const d = wrapLbl(lbl(detail, 9, 'rgba(255,255,255,0.70)')); d.y_align = Clutter.ActorAlign.START; row.add_child(d); }
        return row;
    }

    _buildPanel() {
        const m = this._models; if (!m || !this._panel) return;
        const P = this._panel; this._rememberScroll(); P.destroy_all_children(); this._spinners = [this._barSpin]; this._dlW = {};
        this._rowActors = {}; this._chipActors = {}; this._devActors = {}; this._micRow = this._soundRow = this._listScroll = this._nzFill = null; this._catActors = {};
        const pg = PAGES.some(x => x[0] === this._page) ? this._page : null; this._page = pg;
        this._catSig = this._cat ? JSON.stringify(this._cat.map(e => [e.id, e.state, e.msg || ''])) + this._confirm + JSON.stringify(this._note) : '';
        const loading = this._isLoading(); this._panelLoading = loading;
        const dim = (a) => { if (loading) { a.opacity = 110; a.reactive = false; } return a; };
        const sect = (t, first) => {   // small grey heading with a subtle divider above it
            const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true});
            const l = lbl(t, 10, 'rgba(255,255,255,0.82)', 'bold');
            if (first) { l.style += ' padding: 8px 2px 2px 2px;'; }
            else { box.add_child(new St.Widget({x_expand: true, height: 1, style: 'margin-top: 5px; background-color: rgba(255,255,255,0.07);'})); l.style += ' padding: 7px 2px 2px 2px;'; }
            box.add_child(l); return box;
        };

        // header: title + close (red glow on hover)
        const head = new St.BoxLayout({style: 'spacing: 8px;'});
        if (pg) { this._panelBack = this._iconButton('go-previous-symbolic', 16, false, () => { this._page = null; this._scrollKeep = 0; this._buildPanel(); }); head.add_child(this._panelBack); }
        else this._panelBack = null;
        const title = lbl(pg ? PAGES.find(x => x[0] === pg)[1] : 'Voice typing settings', 13, '#fff', 'bold'); title.x_expand = true; title.y_align = Clutter.ActorAlign.CENTER;
        head.add_child(title);
        this._panelClose = this._iconButton('window-close-symbolic', 16, true, () => this._closePanel());
        head.add_child(this._panelClose);
        P.add_child(head);
        const depsCard = (!pg || pg === 'setup') ? this._depsCard() : null; if (depsCard) P.add_child(depsCard);
        if (!pg) P.add_child(this._homePage(m));

        if (pg === 'model') {

        // run on: Graphics card | Processor (segmented control)
        P.add_child(sect('Run on', true)); this._devActors = {};
        const seg = new St.BoxLayout({x_expand: true, style: 'padding: 3px; spacing: 3px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); background-color: rgba(255,255,255,0.06);'});
        for (const dev of ['gpu', 'cpu']) {
            const on = this._sel.dev === dev, avail = this._entries().some(e => devOf(e) === dev);
            const b = new St.Bin({x_expand: true, child: lbl(DEV_LABEL[dev], 10.5, on ? '#000' : '#fff', on ? 'bold' : null)});
            b.child.x_align = Clutter.ActorAlign.CENTER;
            this._interactive(b, (a, st) => {
                a.style = 'padding: 6px 10px; border-radius: 6px; background-color: ' + (on ? (st.down ? '#35a9e0' : st.hover ? '#6acfff' : ACCENT)
                          : st.down ? 'rgba(255,255,255,0.05)' : st.hover ? 'rgba(255,255,255,0.12)' : 'transparent') + ';';
            }, () => this._pickDevice(dev), () => avail);
            if (!avail) b.opacity = 90;
            dim(b); seg.add_child(b);
            this._devActors[dev] = b;
        }
        P.add_child(seg);
        if (m.note) { const n = wrapLbl(lbl(m.note, 9.5, SEC)); n.style += ' padding: 5px 2px 0 2px;'; P.add_child(n); }

        // models for the chosen device (scrolls if the screen is short)
        P.add_child(sect('Speech model'));
        const mon = Main.layoutManager.primaryMonitor, maxH = Math.max(150, Math.min(520, Math.round((mon ? mon.height : 720) - 470 - 330)));
        const scroll = new St.ScrollView({style: `max-height: ${maxH}px;`, overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC, x_expand: true});
        const list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 2px;'});
        scroll.add_child(list);
        this._rowActors = {};
        const seen = new Set();
        for (const e of this._entries().filter(x => devOf(x) === this._sel.dev && !seen.has(x.id) && seen.add(x.id))) {
            const inst = e.state === 'installed'; const on = inst && this._sel.model === e.id;
            const row = new St.BoxLayout({x_expand: true});
            const radio = new St.Bin({width: 18, height: 18, y_align: Clutter.ActorAlign.START, opacity: inst ? 255 : 90,
                style: `border-radius: 9px; border: 2px solid ${on ? ACCENT : 'rgba(255,255,255,0.55)'}; margin-top: 2px;`});
            if (on) radio.set_child(new St.Widget({width: 8, height: 8, x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER,
                                                   style: `border-radius: 4px; background-color: ${ACCENT};`}));
            const col = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
            col.add_child(wrapLbl(lbl(e.name + (e.id === m.current ? '  ·  current' : ''), 11, '#fff', 'bold')));
            col.add_child(wrapLbl(lbl(`${e.label} · ${e.speed}`, 9.5, SEC)));
            const imp = this._impactRow(e); if (imp) col.add_child(imp);
            if (this._cat) col.add_child(this._actionLine(e, m));
            row.add_child(radio); row.add_child(col);
            this._interactive(row, (a, s) => {
                a.style = 'spacing: 12px; padding: 7px 10px; border-radius: 6px; border: 1px solid ' + (on ? 'rgba(76,194,255,0.38)' : 'transparent') + '; background-color: ' +
                          (on ? (s.down ? 'rgba(76,194,255,0.22)' : 'rgba(76,194,255,0.14)') : s.down ? 'rgba(255,255,255,0.04)' : s.hover ? 'rgba(255,255,255,0.07)' : 'transparent') + ';';
            }, () => { this._sel.model = e.id; this._buildPanel(); }, () => inst);     // a model that is not installed cannot be picked: use its Download button
            if (loading && inst) dim(row); list.add_child(row); this._rowActors[e.id] = row;
        }
        this._listScroll = scroll; P.add_child(scroll); this._restoreScroll();
        }

        if (pg === 'typing') {

        P.add_child(sect('Pause after silence', true));
        const chips = new St.BoxLayout({style: 'spacing: 6px; padding: 2px 0 0 0;'}); this._chipActors = {};
        for (const [v, t] of PAUSES) {
            const on = this._sel.pause === v;
            const chip = new St.Bin({child: lbl(t, 10.5, on ? '#000' : '#fff')});
            this._interactive(chip, (a, s) => {
                a.style = 'padding: 5px 14px; border-radius: 15px; border: 1px solid ' + (on ? 'transparent' : 'rgba(255,255,255,0.07)') + '; background-color: ' +
                          (on ? (s.down ? '#35a9e0' : s.hover ? '#6acfff' : ACCENT) : s.down ? 'rgba(255,255,255,0.05)' : s.hover ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.07)') + ';';
            }, () => { this._sel.pause = v; this._buildPanel(); });
            dim(chip); chips.add_child(chip); this._chipActors[v] = chip;
        }
        P.add_child(chips);
        }

        if (pg === 'mic') {
        P.add_child(sect('Microphone', true));
        const mics = m.settings.mics || [];
        const cur = mics.find(x => x.id === this._sel.mic) || mics[0] || {name: 'System default', id: ''};
        const micRow = new St.BoxLayout({x_expand: true});
        const mname = lbl(cur.name, 10.5, '#fff'); mname.x_expand = true; mname.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        micRow.add_child(mname);
        micRow.add_child(new St.Icon({icon_name: 'pan-down-symbolic', icon_size: 14, style: 'color: rgba(255,255,255,0.85);'}));
        this._interactive(micRow, (a, s) => {
            a.style = 'padding: 8px 10px; border-radius: 6px; spacing: 8px; border: 1px solid rgba(255,255,255,0.07); background-color: ' +
                      (s.down ? 'rgba(255,255,255,0.04)' : s.hover ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.06)') + ';';
        }, () => { const k = mics.findIndex(x => x.id === cur.id); this._sel.mic = mics[(k + 1) % mics.length].id; this._buildPanel(); });
        dim(micRow); P.add_child(micRow); this._micRow = micRow;
        P.add_child(this._noiseSectionSafe(sect, dim));
        }

        if (pg === 'typing') {

        // sounds on/off switch
        P.add_child(sect('Sounds'));
        const on = !!this._sel.sounds;
        const sw = new St.BoxLayout({x_expand: true});
        const sname = lbl('Play start and stop sounds', 10.5, '#fff'); sname.x_expand = true; sname.y_align = Clutter.ActorAlign.CENTER;
        const track = new St.Bin({width: 40, height: 20, y_align: Clutter.ActorAlign.CENTER,
            style: `border-radius: 10px; border: 1px solid ${on ? ACCENT : 'rgba(255,255,255,0.6)'}; background-color: ${on ? ACCENT : 'transparent'};`});
        const knob = new St.Widget({width: 12, height: 12, y_align: Clutter.ActorAlign.CENTER, x_align: on ? Clutter.ActorAlign.END : Clutter.ActorAlign.START,
            style: `border-radius: 6px; margin: 0 4px; background-color: ${on ? '#000' : 'rgba(255,255,255,0.8)'};`});
        track.set_child(knob);
        sw.add_child(sname); sw.add_child(track);
        this._interactive(sw, (a, st) => {
            a.style = 'padding: 8px 10px; border-radius: 6px; spacing: 8px; border: 1px solid rgba(255,255,255,0.07); background-color: ' +
                      (st.down ? 'rgba(255,255,255,0.04)' : st.hover ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.06)') + ';';
        }, () => { this._sel.sounds = !this._sel.sounds; this._buildPanel(); });
        dim(sw); P.add_child(sw); this._soundRow = sw;
        }

        if (pg === 'setup') P.add_child(this._setupInfo(m, !!depsCard));

        // footer: status (spinner / ready / error) + Save
        const foot = new St.BoxLayout({style: 'padding-top: 14px; spacing: 10px;'});
        this._panelSpin = this._mkSpinner(18);
        this._statusLabel = wrapLbl(lbl('', 10, 'rgba(255,255,255,0.85)'));
        const stat = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;', y_align: Clutter.ActorAlign.CENTER});
        stat.add_child(this._panelSpin); stat.add_child(this._statusLabel);
        foot.add_child(stat);
        this._buildSave(); foot.add_child(this._saveBtn);
        P.add_child(foot);
        if (pg === 'setup') P.add_child(this._uninstallSection());
        this._updatePanelStatus(); this._ensureSpin();
        if (this._panel.visible) { this._placePanel(); this._geomSoon(); }
    }

    // ---------- settings home: one clickable tile per category ----------
    _homePage(m) {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 6px; padding-top: 10px;'});
        try {
            const s = m.settings || {}, cur = this._entries().find(e => e.id === this._sel.model);
            const mics = s.mics || [], mic = mics.find(x => x.id === this._sel.mic) || mics[0];
            const pause = (PAUSES.find(x => x[0] === this._sel.pause) || [0, ''])[1];
            const miss = this._deps && this._deps.needed ? (this._deps.total || (this._deps.missing || []).length) : 0;
            const sub = {
                model: (cur ? cur.name : 'No model picked') + '  ·  ' + DEV_LABEL[this._sel.dev],
                mic: (mic ? mic.name : 'System default') + '  ·  noise suppression ' + (s.noise_suppression !== false ? 'on' : 'off'),
                typing: 'Pause after ' + (pause === 'Never' ? 'never' : pause) + '  ·  sounds ' + (this._sel.sounds ? 'on' : 'off'),
                setup: miss ? `${miss} optional package${miss === 1 ? '' : 's'} missing  ·  uninstall` : 'Optional packages  ·  uninstall',
            };
            for (const [id, name, icon] of PAGES) {
                const row = new St.BoxLayout({x_expand: true});
                row.add_child(new St.Icon({icon_name: icon, icon_size: 18, y_align: Clutter.ActorAlign.CENTER, style: `color: ${ACCENT};`}));
                const col = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
                col.add_child(lbl(name, 11, '#fff', 'bold'));
                const d = lbl(sub[id], 9.5, SEC); d.clutter_text.ellipsize = Pango.EllipsizeMode.END; col.add_child(d);
                row.add_child(col);
                row.add_child(new St.Icon({icon_name: 'go-next-symbolic', icon_size: 14, y_align: Clutter.ActorAlign.CENTER, style: 'color: rgba(255,255,255,0.6);'}));
                this._interactive(row, (a, st) => {
                    a.style = 'spacing: 12px; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.07); background-color: ' +
                              (st.down ? 'rgba(255,255,255,0.04)' : st.hover ? 'rgba(255,255,255,0.11)' : 'rgba(255,255,255,0.06)') + ';';
                }, () => { this._page = id; this._scrollKeep = 0; this._unConfirm = false; this._buildPanel(); });
                box.add_child(row); this._catActors[id] = row;
            }
        } catch (e) { console.error('voice-typing settings home: ' + e); }
        return box;
    }
    _setupInfo(m, hasCard) {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 4px; padding-top: 10px;'});
        try {
            if (!hasCard) box.add_child(wrapLbl(lbl('✓ All optional packages are installed.', 10.5, '#7ddc70')));
            const v = m.version || (m.settings || {}).version;
            if (v) box.add_child(lbl('Version ' + v, 9.5, SEC));
        } catch (e) {}
        return box;
    }

    // ---------- background noise + uninstall (own section builders; each is wrapped so a failure here can never break the rest of the panel) ----------
    _noiseSectionSafe(sect, dim) {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true});
        try { box.add_child(sect('Background noise')); box.add_child(this._noiseSection(dim)); }
        catch (e) { console.error('voice-typing noise section: ' + e); }
        return box;
    }
    _noiseSet(payload) {   // noise settings apply at once (no Save): optimistic local update, then the daemon confirms through models.json
        const s = this._models && this._models.settings; if (!s) return;
        Object.assign(s, payload); this._cli(['--apply', JSON.stringify(payload)]); this._buildPanel();
    }
    _noiseSection(dim) {
        const m = this._models, s = m.settings || {}, col = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 6px; padding-top: 2px;'});
        const rowStyle = (a, st) => { a.style = 'padding: 8px 10px; border-radius: 6px; spacing: 8px; border: 1px solid rgba(255,255,255,0.07); background-color: ' +
                                    (st.down ? 'rgba(255,255,255,0.04)' : st.hover ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.06)') + ';'; };
        // 1. noise suppression on/off
        const on = s.noise_suppression !== false, sw = new St.BoxLayout({x_expand: true});
        const sname = lbl('Noise suppression', 10.5, '#fff'); sname.x_expand = true; sname.y_align = Clutter.ActorAlign.CENTER;
        const track = new St.Bin({width: 40, height: 20, y_align: Clutter.ActorAlign.CENTER,
            style: `border-radius: 10px; border: 1px solid ${on ? ACCENT : 'rgba(255,255,255,0.6)'}; background-color: ${on ? ACCENT : 'transparent'};`});
        track.set_child(new St.Widget({width: 12, height: 12, y_align: Clutter.ActorAlign.CENTER, x_align: on ? Clutter.ActorAlign.END : Clutter.ActorAlign.START,
            style: `border-radius: 6px; margin: 0 4px; background-color: ${on ? '#000' : 'rgba(255,255,255,0.8)'};`}));
        sw.add_child(sname); sw.add_child(track);
        this._interactive(sw, rowStyle, () => this._noiseSet({noise_suppression: !on}));
        dim(sw); col.add_child(sw);
        // 2. Auto / Manual noise floor
        const modeRow = new St.BoxLayout({style: 'spacing: 6px;'}), mode = s.noise_mode === 'manual' ? 'manual' : 'auto';
        const mt = lbl('Noise level', 10.5, '#fff'); mt.y_align = Clutter.ActorAlign.CENTER; mt.style += ' padding-right: 6px;'; modeRow.add_child(mt);
        for (const [v, t] of [['auto', 'Auto'], ['manual', 'Manual']]) {
            const sel = mode === v, chip = new St.Bin({child: lbl(t, 10.5, sel ? '#000' : '#fff')});
            this._interactive(chip, (a, st) => {
                a.style = 'padding: 5px 14px; border-radius: 15px; border: 1px solid ' + (sel ? 'transparent' : 'rgba(255,255,255,0.07)') + '; background-color: ' +
                          (sel ? (st.down ? '#35a9e0' : st.hover ? '#6acfff' : ACCENT) : st.down ? 'rgba(255,255,255,0.05)' : st.hover ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.07)') + ';';
            }, () => this._noiseSet({noise_mode: v}));
            dim(chip); modeRow.add_child(chip);
        }
        col.add_child(modeRow);
        // 3. sensitivity
        const sens = Math.max(0, Math.min(1, typeof s.silence_threshold === 'number' ? s.silence_threshold : 0.5));
        const sensRow = new St.BoxLayout({x_expand: true, style: 'spacing: 10px;'});
        const sl = lbl('Sensitivity', 10.5, '#fff'); sl.y_align = Clutter.ActorAlign.CENTER; sensRow.add_child(sl);
        if (this._SliderCls === undefined) {
            this._SliderCls = null;
            import('resource:///org/gnome/shell/ui/slider.js').then(mod => { this._SliderCls = mod.Slider || false; if (this._panelOpen && this._panel) this._buildPanel(); })
                .catch(() => { this._SliderCls = false; if (this._panelOpen && this._panel) this._buildPanel(); });
        }
        if (this._SliderCls) {
            const slider = new this._SliderCls(sens); slider.x_expand = true; slider.y_align = Clutter.ActorAlign.CENTER; slider.reactive = true; slider.can_focus = false;
            const val = lbl(Math.round(sens * 100) + '%', 10, SEC); val.y_align = Clutter.ActorAlign.CENTER; val.width = 40;
            slider.connect('notify::value', () => { val.text = Math.round(slider.value * 100) + '%'; });
            slider.connect('drag-end', () => this._noiseSet({silence_threshold: Math.round(slider.value * 100) / 100}));
            sensRow.add_child(slider); sensRow.add_child(val);
        } else if (this._SliderCls === false) {   // slider widget unavailable: three steps instead
            for (const [v, t] of [[0.25, 'Low'], [0.5, 'Medium'], [0.75, 'High']]) {
                const sel = Math.abs(sens - v) < 0.13, chip = new St.Bin({child: lbl(t, 10.5, sel ? '#000' : '#fff')});
                this._interactive(chip, (a, st) => { a.style = 'padding: 5px 14px; border-radius: 15px; background-color: ' + (sel ? ACCENT : st.hover ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.07)') + ';'; },
                                  () => this._noiseSet({silence_threshold: v}));
                sensRow.add_child(chip);
            }
        } else sensRow.add_child(lbl('…', 10, SEC));
        dim(sensRow); col.add_child(sensRow);
        col.add_child(wrapLbl(lbl('Higher picks up quieter speech. Lower ignores more background noise.', 9, SEC)));
        // 4. live level bar + Calibrate
        const W = 230, bar = new St.Widget({width: W, height: 10, y_align: Clutter.ActorAlign.CENTER, style: 'border-radius: 5px; background-color: rgba(255,255,255,0.10);'});
        this._nzFill = new St.Widget({width: 0, height: 10, style: `border-radius: 5px; background-color: ${ACCENT};`});
        this._nzFloor = new St.Widget({width: 2, height: 14, y: -2, x: 0, style: 'background-color: rgba(255,214,102,0.95);'});
        bar.add_child(this._nzFill); bar.add_child(this._nzFloor); this._nzW = W;
        const meterRow = new St.BoxLayout({x_expand: true, style: 'spacing: 10px; padding-top: 2px;'});
        const ml = lbl('Level', 10.5, '#fff'); ml.y_align = Clutter.ActorAlign.CENTER; meterRow.add_child(ml); meterRow.add_child(bar);
        const cal = new St.Bin({x_expand: true, x_align: Clutter.ActorAlign.END, child: lbl('Calibrate', 10.5, '#fff')});
        const calOk = () => !this._isLoading() && !(this._info && (this._info.state === 'listening' || this._info.calib === 'running'));
        this._interactive(cal, (a, st) => {
            a.style = 'padding: 5px 14px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.10); background-color: ' +
                      (!st.enabled ? 'rgba(255,255,255,0.04)' : st.down ? 'rgba(255,255,255,0.05)' : st.hover ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.09)') + ';';
            a.opacity = st.enabled ? 255 : 110;
        }, () => this._noiseCalibrate(), calOk);
        meterRow.add_child(cal); col.add_child(meterRow);
        this._nzNote = wrapLbl(lbl('', 9, SEC)); col.add_child(this._nzNote);
        this._updateNoiseMeter();
        if (!this._nzTimer) {   // keep the daemon's mic meter alive while the panel is open (it runs ~6 s per poke and only while not listening)
            this._noiseTick();
            this._nzTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
                if (!this._panel || !this._panelOpen) { this._nzTimer = 0; return GLib.SOURCE_REMOVE; }
                this._noiseTick(); return GLib.SOURCE_CONTINUE;
            });
        }
        return col;
    }
    _noiseTick() { if (!this._info || this._info.state !== 'listening') this._cli(['meter']); }
    _noiseCalibrate() {
        this._info = Object.assign({}, this._info, {calib: 'running'}); this._cli(['calibrate']); this._updateNoiseMeter();
        if (this._saveBtn && this._saveBtn._upd) this._saveBtn._upd();
    }
    _updateNoiseMeter() {   // called from the state.json reader (~10x per second while the daemon is metering or listening)
        if (!this._nzFill || !this._panelOpen) return;
        try {
            const i = this._info || {}, s = (this._models && this._models.settings) || {}, mic = Math.max(0, Math.min(1, i.mic || 0)), fl = Math.max(0, Math.min(1, i.floor_lv || 0));
            this._nzFill.width = Math.round(mic * this._nzW);
            this._nzFill.style = `border-radius: 5px; background-color: ${i.speech ? '#6ccb5f' : ACCENT};`;
            this._nzFloor.x = Math.max(0, Math.round(fl * this._nzW) - 1); this._nzFloor.visible = fl > 0;
            const db = v => Math.round(20 * Math.log10(Math.max(v, 1) / 32768));
            let t = '';
            if (i.calib === 'running') t = 'Measuring the room… stay quiet for 2 seconds.';
            else if (i.calib === 'error') t = 'Could not measure: no microphone audio.';
            else if (s.noise_floor > 0) t = `Calibrated room noise: ${db(s.noise_floor_raw || s.noise_floor)} dB` + (s.noise_suppression !== false ? ` (${db(s.noise_floor)} dB after suppression)` : '') + (s.noise_mode === 'manual' ? '.' : '. Used in Manual mode.');
            else t = s.noise_mode === 'manual' ? 'Manual mode needs a measurement: press Calibrate in a quiet moment.' : 'The yellow mark is the noise floor the app is tracking.';
            this._nzNote.text = t;
        } catch (e) {}
    }

    _uninstallSection() {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 8px; padding-top: 14px;'});
        try {
            box.add_child(new St.Widget({x_expand: true, height: 1, style: 'background-color: rgba(255,255,255,0.07);'}));
            if (!this._unConfirm) {
                const b = new St.Bin({x_expand: true, child: lbl('Uninstall Voice Typing', 10.5, '#fff', 'bold')}); b.child.x_align = Clutter.ActorAlign.CENTER;
                this._interactive(b, (a, st) => { a.style = 'padding: 7px 14px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.12); background-color: ' + (st.down ? CLOSE_RED_DOWN : st.hover ? '#e5484d' : CLOSE_RED) + ';'; },
                                  () => { this._unConfirm = true; this._buildPanel(); });
                box.add_child(b);
            } else {
                box.add_child(wrapLbl(lbl('Remove Voice Typing and all its models and settings?', 11, '#fff', 'bold')));
                const row = new St.BoxLayout({x_expand: true, style: 'spacing: 10px;'});
                const mk = (text, danger, fn) => {
                    const b = new St.Bin({x_expand: true, child: lbl(text, 10.5, '#fff', danger ? 'bold' : null)}); b.child.x_align = Clutter.ActorAlign.CENTER;
                    this._interactive(b, (a, st) => { a.style = 'padding: 7px 14px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.12); background-color: ' +
                        (danger ? (st.down ? CLOSE_RED_DOWN : st.hover ? '#e5484d' : CLOSE_RED) : (st.down ? 'rgba(255,255,255,0.05)' : st.hover ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.09)')) + ';'; }, fn);
                    return b;
                };
                row.add_child(mk('Cancel', false, () => { this._unConfirm = false; this._buildPanel(); }));
                row.add_child(mk('Remove', true, () => this._runUninstall()));
                box.add_child(row);
            }
        } catch (e) { console.error('voice-typing uninstall section: ' + e); }
        return box;
    }
    _runUninstall() {   // runs the installed uninstall.sh fully detached (own session, from a temp copy), so it survives the extension and the daemon being removed
        this._unConfirm = false;
        try {
            const cli = this._models && this._models.cli; if (!cli) return;
            const script = GLib.path_get_dirname(GLib.path_get_dirname(cli)) + '/uninstall.sh';
            GLib.spawn_async(null, ['setsid', '-f', script, '--yes', '--pkexec'], null, GLib.SpawnFlags.SEARCH_PATH, null);
        } catch (e) { console.error('voice-typing uninstall: ' + e); }
        this._closePanel();
    }

    // Save button: dimmed (nothing to save / loading) -> normal -> hover -> pressed -> "Saved" (check) -> dimmed
    _buildSave() {
        const content = new St.BoxLayout({x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 6px;'});
        const check = new St.Icon({icon_name: 'object-select-symbolic', icon_size: 16, y_align: Clutter.ActorAlign.CENTER});
        const text = lbl('Save', 10.5, '#001a26', 'bold');
        content.add_child(check); content.add_child(text);
        const btn = new St.Bin({child: content});
        const enabled = () => this._saveState === 'idle' && this._dirty() && !this._isLoading();
        this._interactive(btn, (a, s) => {
            const saved = this._saveState === 'saved';
            check.visible = saved; text.text = saved ? 'Saved' : 'Save';
            const base = 'min-width: 74px; padding: 6px 18px; border-radius: 6px; ';
            let fg, st;
            if (saved) { fg = '#05230a'; st = 'background-color: #6ccb5f; border: 1px solid rgba(255,255,255,0.18); box-shadow: 0 1px 2px rgba(0,0,0,0.30);'; }
            else if (!s.enabled) { fg = 'rgba(255,255,255,0.36)'; st = 'background-color: rgba(76,194,255,0.16); border: 1px solid rgba(255,255,255,0.04);'; }
            else if (s.down) { fg = '#001a26'; st = 'background-color: #3aa6de; border: 1px solid rgba(0,0,0,0.20); box-shadow: inset 0 2px 4px rgba(0,0,0,0.35); padding-top: 7px; padding-bottom: 5px;'; }
            else if (s.hover) { fg = '#001a26'; st = 'background-color: #6acfff; border: 1px solid rgba(255,255,255,0.30); box-shadow: 0 0 8px rgba(76,194,255,0.40), 0 1px 2px rgba(0,0,0,0.30);'; }
            else { fg = '#001a26'; st = 'background-color: #4cc2ff; border: 1px solid rgba(255,255,255,0.16); box-shadow: 0 1px 2px rgba(0,0,0,0.35);'; }
            a.style = base + st; text.style = `${FONT} font-size: 10.5pt; font-weight: bold; color: ${fg};`; check.style = `color: ${fg};`;
        }, () => this._save(), enabled);
        this._saveBtn = btn;
    }
    _refreshSave() { if (this._saveBtn && this._saveBtn._upd) this._saveBtn._upd(); this._writeGeom(); }

    _updatePanelStatus() {
        if (!this._statusLabel || !this._models) return;
        const sw = this._models.switch || {}, i = this._info || {};
        const loading = this._isLoading();
        if (this._wasLoading && !loading && this._panelOpen && sw.status === 'ready') {   // "Ready" lingers for a few seconds only
            this._readyUntil = GLib.get_monotonic_time() + 3e6;
            if (this._readyTimer) GLib.source_remove(this._readyTimer);
            this._readyTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3100, () => { this._readyTimer = 0; if (this._statusLabel && this._panelOpen) this._updatePanelStatus(); return GLib.SOURCE_REMOVE; });
        }
        this._wasLoading = loading;
        this._panelSpin.visible = loading;
        const sty = (c, sz, b) => `${FONT} font-size: ${sz}pt; color: ${c};${b ? ' font-weight: bold;' : ''}`;
        if (loading) { this._statusLabel.text = `Loading ${i.loading || sw.target}…`; this._statusLabel.style = sty(ACCENT, 10); }
        else if (sw.status === 'error') { this._statusLabel.text = sw.msg; this._statusLabel.style = sty('#ff8a8d', 10); }
        else if (sw.status === 'ready' && GLib.get_monotonic_time() < this._readyUntil) { this._statusLabel.text = '✓ Ready'; this._statusLabel.style = sty('#7ddc70', 10.5, true); }
        else { this._statusLabel.text = ''; }
        this._ensureSpin(); this._refreshSave();
    }

    _save() {
        if (this._isLoading() || this._saveState !== 'idle' || !this._dirty()) return;
        const m = this._models, payload = {};
        if (this._sel.model !== m.current) { payload.model = this._sel.model; payload.device = devOf(this._entries().find(x => x.id === this._sel.model)); }
        if (this._sel.pause !== m.settings.auto_pause_seconds) payload.auto_pause_seconds = this._sel.pause;
        if (this._sel.mic !== (m.settings.mic || '')) payload.mic = this._sel.mic;
        if (this._sel.sounds !== (m.settings.sounds !== false)) payload.sounds = this._sel.sounds;
        this._base = Object.assign({}, this._sel);          // what is saved now; any further change re-activates Save
        this._saveState = 'saved';
        if (this._saveTimer) GLib.source_remove(this._saveTimer);
        this._saveTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1400, () => { this._saveTimer = 0; this._saveState = 'idle'; this._refreshSave(); return GLib.SOURCE_REMOVE; });
        if (payload.model) {   // optimistic: show spinner at once; daemon confirms via models.json/state.json
            const e = this._entries().find(x => x.id === payload.model);
            this._info = Object.assign({}, this._info, {loading: e ? e.name : payload.model});
            m.switch = {status: 'loading', target: payload.model, msg: ''};
            this._buildPanel(); this._apply(this._state, 0, '', false);
        } else this._refreshSave();
        if (Object.keys(payload).length) this._cli(['--apply', JSON.stringify(payload)]);
    }
}
