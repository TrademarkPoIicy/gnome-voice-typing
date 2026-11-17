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
               ['typing', 'Typing & sounds', 'input-keyboard-symbolic'], ['appearance', 'Appearance', 'preferences-desktop-appearance-symbolic'],
               ['formatting', 'Dictation formatting', 'format-text-bold-symbolic'], ['vocabulary', 'Custom vocabulary', 'accessories-dictionary-symbolic'],
               ['shortcuts', 'Keyboard shortcuts', 'preferences-desktop-keyboard-shortcuts-symbolic'], ['setup', 'Setup & uninstall', 'emblem-system-symbolic'], ['developer', 'Developer tools', 'applications-engineering-symbolic']];
const shortName = (n) => (n || '').replace(/\s*\(.*?\)/g, '').replace(/-TDT.*$/, '').replace(/ q5$/, '').trim();

const THEMES = {dark: '32,32,32', light: '245,245,245', contrast: '0,0,0', ocean: '15,43,65', forest: '22,48,35', plum: '51,29,61', midnight: '18,22,40', warm: '57,39,29'};
const THEME_OPTIONS = Object.keys(THEMES).map(key => [key, key === 'contrast' ? 'High contrast' : key[0].toUpperCase() + key.slice(1)]);
const PREFS = {developer_logging: false, bar_theme: 'dark', settings_theme: 'dark', bar_size: 'compact', bar_opacity: 97, show_model: true, punctuation: 'model', capitalization: 'model', spoken_punctuation: false, trailing_space: true, vocabulary: []};
const clone = value => JSON.parse(JSON.stringify(value));

const SEC = 'rgba(255,255,255,0.74)';      // secondary text on the dark panel (~8:1 contrast)
function wrapLbl(l) { const t = l.clutter_text; t.line_wrap = true; t.line_wrap_mode = Pango.WrapMode.WORD_CHAR; t.ellipsize = Pango.EllipsizeMode.NONE; l.x_expand = true; return l; }

function lbl(text, size, color, weight) {
    return new St.Label({text, style: `${FONT} font-size: ${size}pt; color: ${color};${weight ? ' font-weight: ' + weight + ';' : ''}`});
}

export default class VoiceTypingOverlay extends Extension {
    enable() {
        GLib.mkdir_with_parents(DIR, 0o700);
        this._settingsSince = Date.now() / 1000; this._settingsRequest = 0;
        this._editing = false; this._editGrab = null; this._dropOpen = null; this._saveError = ''; this._bindings = null; this._shortcutError = '';
        this._state = 'hidden'; this._last = 0; this._info = {}; this._models = null; this._panelOpen = false;
        this._sel = {model: '', pause: 60, mic: '', sounds: true, follow: false, dev: 'gpu'}; this._base = Object.assign({}, this._sel);
        this._spinners = []; this._spinId = 0; this._geomKey = ''; this._placed = false; this._dragPos = null; this._curKey = null;
        this._saveState = 'idle'; this._saveTimer = 0; this._readyUntil = 0; this._readyTimer = 0; this._wasLoading = false;
        this._dragging = false; this._capId = 0; this._grab = null; this._panelIdle = 0; this._locTok = 0; this._rawPrev = 'hidden'; this._locating = false; this._following = false;
        this._field = null; this._target = null; this._followId = 0; this._fsTimer = 0; this._focusSig = 0; this._userMoved = false;
        // one-click model downloads: catalog (every model + its state) comes from `voice-typing-models status --json`, progress from models/.status/<id>.json
        this._cat = null; this._dlW = {}; this._pend = {}; this._confirm = ''; this._note = {}; this._dlTimer = 0; this._catTimer = 0; this._adopted = false; this._catSig = ''; this._scrollKeep = 0;
        // first-launch check for missing system packages (graphics-card engine / typing): `voice-typing-models deps --json`, progress in models/.status/_deps.json
        this._deps = null; this._depsChecked = false; this._depsTimer = 0; this._depsPend = 0; this._depsAckPending = false; this._depsRevision = 0;

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
        // no model installed yet: the model line reads 'Install model' and opens Settings > Speech model (otherwise clicks pass through to the bar)
        this._modelLabel.reactive = true; this._modelLabel.track_hover = true;
        const noModel = () => !!(this._info && this._info.no_model);
        this._modelLabel.connect('notify::hover', () => { if (noModel()) this._modelLabel.style = this._modelStyle(this._modelLabel.hover ? '#fff' : ACCENT, true, 7.5); });
        this._modelLabel.connect('button-press-event', (a, ev) => (noModel() && ev.get_button() === 1) ? Clutter.EVENT_STOP : Clutter.EVENT_PROPAGATE);
        this._modelLabel.connect('button-release-event', (a, ev) => { if (!noModel() || ev.get_button() !== 1) return Clutter.EVENT_PROPAGATE; this._openModelPage(); return Clutter.EVENT_STOP; });
        this._barSpin = this._mkSpinner(12); this._barSpin.visible = false;
        this._gear = this._iconButton('emblem-system-symbolic', 12, false, () => this._togglePanel(), 24);
        this._close = this._iconButton('window-close-symbolic', 12, true, () => { this._closePanel(); this._cli(['close']); }, 24);
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
        this._endEditing();
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
        const upd = () => {
            styleFn(actor, {hover: actor.hover && en(), down: down && en(), enabled: en()});
            if (this._models?.settings?.settings_theme === 'light' && this._panel?.contains(actor)) this._lightenPanelActor(actor);
        };
        actor.connect('notify::hover', () => { if (!actor.hover) down = false; upd(); });
        actor.connect('button-press-event', (a, ev) => { if (ev.get_button() !== 1) return Clutter.EVENT_PROPAGATE; down = true; upd(); return Clutter.EVENT_STOP; });
        actor.connect('button-release-event', (a, ev) => {
            const was = down; down = false; upd();
            if (was && actor.hover && en()) { this._log('CONTROL CLICK', this._page || 'dictation bubble'); onClick(); }
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
            else if (s.down) { a.style = `border-radius: ${R}px; background-color: rgba(255,255,255,0.04);`; icon.style = a === this._gear && this._appearance().theme === 'light' ? 'color: #222;' : 'color: rgba(255,255,255,0.6);'; }
            else if (s.hover) { a.style = `border-radius: ${R}px; background-color: rgba(255,255,255,0.09);`; icon.style = (a === this._gear || a === this._close) && this._appearance().theme === 'light' ? 'color: #111;' : 'color: rgba(255,255,255,0.95);'; }
            else { a.style = `border-radius: ${R}px; background-color: transparent;`; icon.style = (a === this._gear || a === this._close) && this._appearance().theme === 'light' ? 'color: #333;' : 'color: rgba(255,255,255,0.68);'; }
        }, onClick);
    }

    _appearance() {
        const s = this._models?.settings || {};
        return {theme: Object.hasOwn(THEMES, s.bar_theme) ? s.bar_theme : 'dark',
                size: ['tiny', 'compact', 'comfortable', 'large'].includes(s.bar_size) ? s.bar_size : 'compact',
                opacity: Number.isFinite(s.bar_opacity) ? Math.max(0, Math.min(100, s.bar_opacity)) / 100 : 0.97,
                showModel: s.show_model !== false};
    }
    _barStyle(hot) {
        const a = this._appearance(), light = a.theme === 'light', contrast = a.theme === 'contrast';
        const rgb = THEMES[a.theme];
        // A fixed dark shadow shows through a translucent fill and looks like the
        // default dark theme. Fade every part of the backdrop together.
        const alpha = value => (value * a.opacity).toFixed(4);
        const borderRgb = light ? '0,0,0' : '255,255,255';
        const border = `rgba(${borderRgb},${alpha(contrast ? 1 : light ? 0.25 : hot ? 0.16 : 0.08)})`;
        const shadow = a.opacity === 0 ? 'none' : `0 4px 14px rgba(0,0,0,${alpha(0.5)})`;
        return `background-color: rgba(${rgb},${a.opacity.toFixed(4)}); border: 1px solid ${border}; border-radius: 22px; ` +
               `padding: 4px 5px 4px 9px; spacing: 4px; box-shadow: ${shadow};`;
    }
    _applyAppearance() {
        if (!this._bar || !this._label) return;
        const a = this._appearance(), light = a.theme === 'light';
        const size = {tiny: [102, 8, 24], compact: [204, 9, 24], comfortable: [260, 11, 30], large: [320, 13, 36]}[a.size];
        this._bar.width = size[0]; this._bar.style = this._barStyle(this._bar.hover);
        this._label.style = `${FONT} font-size: ${size[1]}pt; color: ${light ? '#171717' : '#fff'};`;
        this._label.get_parent().visible = a.size !== 'tiny';
        this._grip.visible = a.size !== 'tiny';
        this._modelLabel.visible = a.size !== 'tiny' && (a.showModel || !!this._info.no_model || !!this._info.loading);
        for (const button of [this._mic, this._gear, this._close]) { button.width = size[2]; button.height = size[2]; }
        this._grip.style = `color: ${light ? '#555' : 'rgba(255,255,255,0.55)'};`;
        // Keep all action icons readable on a light background, including hover states.
        for (const button of [this._gear, this._close]) { if (button._upd) button._upd(); }
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
        this._log('COMMAND', JSON.stringify(args));
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
        if (d.settings_request > this._settingsSince && d.settings_request !== this._settingsRequest && this._models) { this._settingsRequest = d.settings_request; this._openPanel(); }
    }

    _readModels() {
        try {
            const [ok, bytes] = GLib.file_get_contents(DIR + '/models.json');
            if (!ok || !bytes.length) return;
            const m = JSON.parse(new TextDecoder().decode(bytes));
            const first = !this._models;
            this._models = m;
            this._applyAppearance();
            const requested = this._info?.settings_request;
            if (requested > this._settingsSince && requested !== this._settingsRequest) { this._settingsRequest = requested; this._openPanel(); }
            if (first || !this._panelOpen) this._syncSel();
            if (this._panelOpen) this._buildPanel();
        } catch (e) {}
    }
    _syncSel() {
        const m = this._models; if (!m) return;
        this._sel = {model: m.current, pause: m.settings.auto_pause_seconds, mic: m.settings.mic || '', sounds: m.settings.sounds !== false, follow: m.settings.follow_field === true,
                     dev: devOf(this._entries().find(e => e.id === m.current) || (m.models || []).find(e => e.id === m.current) || {device: m.settings.device})};
        this._base = Object.assign({}, this._sel);
        this._prefDraft = clone(PREFS);
        for (const key of Object.keys(PREFS)) if (m.settings[key] !== undefined) this._prefDraft[key] = clone(m.settings[key]);
        this._basePrefs = clone(this._prefDraft);
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
    _spawnTool(args) {
        this._log('SETUP COMMAND', JSON.stringify(args));   // detached helper process (own session): downloads never run inside gnome-shell and survive it
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
    _refreshDeps() {
        if (this._depsAckPending || this._depsReading) return;
        const revision = ++this._depsRevision;
        const tool = this._toolPath('voice-typing-models'); if (!tool || !GLib.file_test(tool, GLib.FileTest.EXISTS)) return;
        let p; try { p = Gio.Subprocess.new([tool, 'deps', '--json'], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE); } catch (e) { return; }
        this._depsReading = true;
        p.communicate_utf8_async(null, null, (pr, res) => {
            try {
                if (!this._bar || revision !== this._depsRevision) return;
                const [, out] = pr.communicate_utf8_finish(res);
                const d = JSON.parse(out); const prev = JSON.stringify(this._deps); this._deps = d;
                d.jobs ||= {};
                for (const [id, started] of Object.entries(this._packagePending || {})) {
                    if (d.jobs[id]?.t >= started) delete this._packagePending[id];
                    else if (Date.now() / 1000 - started < 15) d.jobs[id] = {status: 'installing', component: id, msg: 'Starting…', pct: 0};
                    else { delete this._packagePending[id]; d.jobs[id] = {status: 'failed', component: id, msg: 'Installer did not start. Retry this package.'}; }
                }
                if (Object.values(d.jobs).some(j => this._packageActive(j))) this._depsTickStart();
                // Background checks update Settings only when the user has opened it.
                if (this._panelOpen && prev !== JSON.stringify(d)) this._buildPanel();
            } catch (e) { console.error('voice-typing deps: ' + e); }
            finally { this._depsReading = false; }
        });
    }
    _packageActive(job) { return ['installing', 'building', 'downloading', 'verifying'].includes(job?.status); }
    _depsTickStart() { if (!this._depsTimer) this._depsTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => this._depsTick()); }
    _depsTick() {
        if (!this._bar) { this._depsTimer = 0; return GLib.SOURCE_REMOVE; }
        this._refreshDeps();
        if (Object.values(this._deps?.jobs || {}).some(j => this._packageActive(j)) || Object.keys(this._packagePending || {}).length) return GLib.SOURCE_CONTINUE;
        this._depsTimer = 0; this._refreshCat(); return GLib.SOURCE_REMOVE;
    }
    _depsInstall(component) {
        if (!this._deps || !component || component.installed || !component.can_install) return;
        if (this._packageActive(this._deps.jobs?.all)) return;
        if (component.id === 'all' && Object.values(this._deps.jobs || {}).some(j => this._packageActive(j))) return;
        if (component.resolve_first) component = {id: 'catalog', name: 'package list', can_install: true};
        if (this._packageActive(this._deps.jobs?.[component.id])) return;
        ++this._depsRevision;
        this._deps.jobs ||= {}; this._packagePending ||= {};
        const started = Date.now() / 1000 - 0.1;
        this._packagePending[component.id] = started;
        this._deps.jobs[component.id] = {status: 'installing', component: component.id, msg: 'Preparing ' + component.name + '…', pct: 0};
        if (!this._spawnTool(['deps', 'install', component.id])) {
            delete this._packagePending[component.id];
            this._deps.jobs[component.id] = {status: 'failed', component: component.id, msg: 'Could not start the installer.'};
        }
        this._buildPanel(); this._depsTickStart();
    }
    _depsAcknowledge() {
        if (this._depsAckPending) return;
        const tool = this._toolPath('voice-typing-models'); if (!tool) return;
        let p;
        try { p = Gio.Subprocess.new([tool, 'deps', 'ack'], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE); }
        catch (e) { console.error('voice-typing setup acknowledgement: ' + e); return; }
        this._depsAckPending = true;
        ++this._depsRevision; // Ignore checks started before OK was clicked.
        p.wait_check_async(null, (pr, res) => {
            try {
                pr.wait_check_finish(res);
                if (this._bar && this._deps) this._deps.job = null;
            } catch (e) { console.error('voice-typing setup acknowledgement: ' + e); }
            this._depsAckPending = false;
            if (this._bar) { if (this._panelOpen) this._buildPanel(); this._refreshDeps(); }
        });
    }
    _depsNotNow() {
        this._spawnTool(['deps', 'dismiss']);
        if (this._deps) { this._deps.dismissed = true; this._deps.job = null; }
        this._buildPanel();
    }
    // Each package has its own action. The home page links to the full setup list.
    _depsCard() {
        const d = this._deps; if (!d) return null;
        const jobs = d.jobs || {}, busy = Object.values(jobs).some(j => this._packageActive(j));
        const catalog = jobs.catalog, job = catalog?.status, batch = jobs.all, batchActive = this._packageActive(batch);
        const setupPage = this._page === 'setup';
        if (!setupPage && !d.needed && !busy && job !== 'failed') return null;
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true,
            style: 'margin-top: 8px; padding: 9px 11px; spacing: 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.15);'});
        const note = (text, color = SEC) => wrapLbl(lbl(text, 9.5, color));
        if (!setupPage) {
            box.add_child(note(busy ? 'Installing selected packages…' : 'Choose optional packages individually in Setup & uninstall.'));
            box.add_child(this._pill('View packages', 'plain', () => { this._page = 'setup'; this._scrollKeep = 0; this._buildPanel(); }));
            return box;
        }
        box.add_child(lbl('Packages & setup', 11, '#fff', 'bold'));
        box.add_child(note('Finish setup with Install all, or choose packages individually. Installed packages stay out of this list.'));
        if (d.needed || !d.plan_ready || batchActive || batch?.status === 'failed') {
            box.add_child(this._pill(batchActive ? 'Installing all…' : batch?.status === 'failed' ? 'Retry install all' : 'Install all', 'primary',
                () => this._depsInstall({id: 'all', name: 'optional setup', can_install: true}), !busy));
            box.add_child(note('Includes supporting packages, typing permission and the graphics engine when available. Speech models are selected separately.'));
        }
        if (batch && batch.status !== 'done') box.add_child(note(batch.msg || 'Preparing setup…', batch.status === 'failed' ? '#ff9a9c' : SEC));
        box.add_child(this._pill('Refresh package list', 'plain', () => this._depsInstall({id: 'catalog', name: 'package list', can_install: true}), !batchActive && !this._packageActive(catalog)));
        if (catalog && job !== 'done') box.add_child(note(catalog.msg || 'Working…', job === 'failed' ? '#ff9a9c' : '#fff'));
        if (d.pip_ready === false && !batchActive) box.add_child(note('Install all prepares pip automatically. For individual installs, install pip first and then refresh.'));
        if (d.check_error) box.add_child(note(d.check_error, '#ff9a9c'));
        if (!d.check_error && d.components && !d.components.length) box.add_child(note(d.plan_ready ? 'All listed packages are installed.' : 'Refresh the package list to discover available add-ons and supporting libraries.'));
        if (!d.components) {
            box.add_child(note('Log out and back in after updating Voice typing to load the package list.'));
            return box;
        }
        const mon = Main.layoutManager.primaryMonitor;
        const maxH = Math.max(140, Math.min(340, (mon?.height || 720) - 440));
        const scroll = new St.ScrollView({style: `max-height: ${maxH}px;`, overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC, x_expand: true});
        const list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 12px;'});
        for (const item of d.components.filter(item => !item.installed)) {
            const itemJob = jobs[item.id], active = this._packageActive(itemJob);
            const row = new St.BoxLayout({x_expand: true, style: 'spacing: 10px;'});
            const text = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 3px;'});
            text.add_child(wrapLbl(lbl(item.name, 10.5, '#fff', 'bold')));
            text.add_child(note(item.description));
            if (!item.installed && item.note) text.add_child(note(item.note));
            row.add_child(text);
            if (item.installed) row.add_child(lbl('Installed', 9, '#7ddc70'));
            else {
                const button = this._pill(active ? 'Installing…' : itemJob?.status === 'failed' ? 'Retry' : item.resolve_first ? 'List requirements' : 'Install', 'primary',
                    () => this._depsInstall(item), !batchActive && !active && item.can_install);
                button.y_align = Clutter.ActorAlign.START; row.add_child(button);
            }
            list.add_child(row);
            if (active || itemJob?.status === 'failed') {
                const pct = Number.isFinite(itemJob.pct) ? Math.max(0, Math.min(100, itemJob.pct)) : null;
                const track = new St.BoxLayout({width: 340, height: 6, style: 'border-radius: 3px; background-color: rgba(255,255,255,0.15);'});
                track.add_child(new St.Widget({width: pct === null ? 34 : Math.round(340 * pct / 100), height: 6, style: `border-radius: 3px; background-color: ${ACCENT};`}));
                list.add_child(track);
                list.add_child(note((pct === null ? '' : `${pct}% · `) + (itemJob.msg || 'Working…'), itemJob.status === 'failed' ? '#ff9a9c' : SEC));
            }
            if (!item.installed && !item.can_install && item.manual) {
                const manual = note('Run in a terminal: ' + item.manual); manual.clutter_text.selectable = true; list.add_child(manual);
            }
        }
        scroll.add_child(list); box.add_child(scroll);
        this._listScroll = scroll; this._restoreScroll();
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
    _dirty() { const a = this._sel, b = this._base; return a.model !== b.model || a.pause !== b.pause || a.mic !== b.mic || a.sounds !== b.sounds || a.follow !== b.follow || JSON.stringify(this._prefDraft) !== JSON.stringify(this._basePrefs); }

    // ---------- position (drag) ----------
    // The dragged position lives only in this._dragPos for the current open session (cleared by _hide); nothing is saved or reloaded.
    _log(kind, extra) {
        if (this._models?.settings?.developer_logging !== true) return;
        const tool = this._toolPath('voice-typing-logs'); if (!tool) return;
        try { Gio.Subprocess.new([tool, 'event', kind, extra || ''], Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE); }
        catch (e) { console.error('Voice typing diagnostics: ' + e); }
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
    _barSize() { return [this._bar.width || BAR_W, this._bar.get_preferred_height(-1)[1]]; }

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
        if (!this._bar || this._state === 'hidden' || !this._placed || this._locating || this._following || this._dragging || this._editing || this._panelOpen) return;
        this._following = true;
        this._locate(0.25, (f) => {
            this._following = false;
            if (!this._bar || this._state === 'hidden') return;
            if (this._models && this._models.settings && this._models.settings.follow_field !== true) return;   // 'Follow text fields' off: stay put
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
                if (this._followRow) g.follow = c(this._followRow);
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
    _modelStyle(color, bold, pt = 8) {
        const a = this._appearance();
        if (a.theme === 'light') color = color === ACCENT ? '#005a85' : '#444';
        pt += a.size === 'large' ? 3 : a.size === 'comfortable' ? 1.5 : 0;
        return `${FONT} font-size: ${pt}pt; color: ${color};${bold ? ' font-weight: bold;' : ''}`;
    }

    _apply(state, level, text, busy) {
        if (!this._bar) return;
        if (this._loggedState !== state) { this._log('OVERLAY STATE', state); this._loggedState = state; }
        if (state === 'hidden' || state === 'suspended') { if (this._panelOpen) return; this._chimeFor(state); this._hide(); return; }   // suspended: the bar shows nothing
        this._chimeFor(state);
        this._state = state;
        const i = this._info, loading = !!i.loading;
        const dv = i.device ? ' · ' + i.device : '';
        if (i.no_model) { this._modelLabel.text = 'Install model ›'; this._modelLabel.style = this._modelStyle(this._modelLabel.hover ? '#fff' : ACCENT, true, 7.5); }
        else if (loading) { this._modelLabel.text = `Loading ${shortName(i.loading)}…`; this._modelLabel.style = this._modelStyle(ACCENT); }
        // 'Now <model>' at 7pt fits the 204 px bar (the long 'Switched to ...' was cut off); ellipsize END still covers long names
        else if (i.switched) { this._modelLabel.text = `Now ${shortName(i.switched)}${dv}`; this._modelLabel.style = this._modelStyle(ACCENT, true, 7); }
        else { this._modelLabel.text = i.model ? shortName(i.model) + dv : ''; this._modelLabel.style = this._modelStyle('rgba(255,255,255,0.55)'); }
        this._barSpin.visible = loading; this._ensureSpin();
        if (state === 'listening') {
            this._label.text = text || 'Listening…';
            this._setMic(i.no_model || busy ? '#ffd166' : ACCENT, 'audio-input-microphone-symbolic', '#000', Math.round(level * 5), loading);
        } else if (state === 'starting') {
            this._label.text = text || 'Getting ready…'; this._setMic(IDLE_BG, 'audio-input-microphone-symbolic', this._appearance().theme === 'light' ? '#333' : '#e8e8e8', 0, loading);
        } else if (state === 'paused') {
            this._label.text = text || 'Paused';
            this._setMic(i.mic_active ? '#ffd166' : IDLE_BG, i.mic_active ? 'audio-input-microphone-symbolic' : 'microphone-disabled-symbolic', i.mic_active ? '#000' : this._appearance().theme === 'light' ? '#333' : '#e8e8e8', i.mic_active ? Math.round(level * 5) : 0, loading);
        } else {
            this._label.text = text || 'Something went wrong'; this._setMic('#e5484d', 'dialog-warning-symbolic', '#fff', 0, false);
        }
        this._applyAppearance();
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
        if (!this._depsChecked && this._toolPath('voice-typing-models')) { this._depsChecked = true; this._refreshDeps(); }
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
        if (!this._panelOpen && (!alive || (this._state === 'listening' && age > 4))) { this._closePanel(); this._hide(); }
    }

    // ---------- settings panel ----------
    _openModelPage() {
        if (!this._panelOpen) this._openPanel();
        this._page = 'model'; this._scrollKeep = 0; this._unConfirm = false; this._buildPanel();
    }
    _togglePanel() { this._panelOpen ? this._closePanel() : this._openPanel(); }
    _openPanel() {
        if (!this._models || this._panelOpen) return;
        this._syncSel(); this._saveState = 'idle'; this._readyUntil = 0;
        if (this._saveTimer) { GLib.source_remove(this._saveTimer); this._saveTimer = 0; }
        this._dropOpen = null; this._saveError = '';
        this._confirm = ''; this._note = {}; this._scrollKeep = 0; this._page = this._page === 'setup' ? 'setup' : null; this._unConfirm = false; this._panelOpen = true; this._buildPanel(); this._refreshCat(); this._refreshDeps();
        this._panel.visible = true; this._panel.reactive = true; this._placePanel(); this._geomSoon();
        this._panel.remove_all_transitions(); this._panel.ease({opacity: 255, duration: 120, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }
    _closePanel() {
        this._endEditing(); this._dropOpen = null; this._opacityInvalid = false;
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
            const l = line(); l.add_child(this._pill('Set up', 'primary', () => { this._page = 'setup'; this._buildPanel(); this._refreshDeps(); })); box.add_child(l);
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
        if (this._editing) return;
        const m = this._models; if (!m || !this._panel) return;
        const P = this._panel; this._rememberScroll(); P.destroy_all_children(); this._spinners = [this._barSpin]; this._dlW = {};
        this._rowActors = {}; this._chipActors = {}; this._devActors = {}; this._micRow = this._soundRow = this._followRow = this._listScroll = this._nzFill = null; this._catActors = {};
        const pg = PAGES.some(x => x[0] === this._page) ? this._page : null; this._page = pg;
        if (this._loggedPage !== pg) { this._log('SETTINGS PAGE', pg || 'home'); this._loggedPage = pg; }
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
        if (pg) { this._panelBack = this._iconButton('go-previous-symbolic', 16, false, () => { this._endEditing(); this._dropOpen = null; this._opacityInvalid = false; this._page = null; this._scrollKeep = 0; this._buildPanel(); }); head.add_child(this._panelBack); }
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
        const mics = [...(m.settings.mics || [])];
        if (!mics.some(mic => mic.id === '')) mics.unshift({id: '', name: 'System default'});
        if (!mics.some(mic => mic.id === this._sel.mic)) mics.push({id: this._sel.mic, name: 'Disconnected microphone (choose another)'});
        P.add_child(this._dropdown('microphone', mics.map(mic => [mic.id, mic.name]), this._sel.mic,
            value => { this._sel.mic = value; }, loading));
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

        {
        // follow-the-text-field on/off switch
        P.add_child(sect('Bar position'));
        const on = !!this._sel.follow;
        const sw = new St.BoxLayout({x_expand: true});
        const sname = lbl('Move bar to the text field I click', 10.5, '#fff'); sname.x_expand = true; sname.y_align = Clutter.ActorAlign.CENTER;
        const track = new St.Bin({width: 40, height: 20, y_align: Clutter.ActorAlign.CENTER,
            style: `border-radius: 10px; border: 1px solid ${on ? ACCENT : 'rgba(255,255,255,0.6)'}; background-color: ${on ? ACCENT : 'transparent'};`});
        const knob = new St.Widget({width: 12, height: 12, y_align: Clutter.ActorAlign.CENTER, x_align: on ? Clutter.ActorAlign.END : Clutter.ActorAlign.START,
            style: `border-radius: 6px; margin: 0 4px; background-color: ${on ? '#000' : 'rgba(255,255,255,0.8)'};`});
        track.set_child(knob);
        sw.add_child(sname); sw.add_child(track);
        this._interactive(sw, (a, st) => {
            a.style = 'padding: 8px 10px; border-radius: 6px; spacing: 8px; border: 1px solid rgba(255,255,255,0.07); background-color: ' +
                      (st.down ? 'rgba(255,255,255,0.04)' : st.hover ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.06)') + ';';
        }, () => { this._sel.follow = !this._sel.follow; this._buildPanel(); });
        dim(sw); P.add_child(sw); this._followRow = sw;
        }
        }

        if (pg === 'mic' && this._info.no_model) P.add_child(wrapLbl(lbl('Microphone preview works without a model. Install a model in Speech model to turn detected speech into text.', 9.5, SEC)));
        if (pg === 'appearance') this._appearanceSettings(P, sect);
        if (pg === 'formatting') this._formattingSettings(P, sect);
        if (pg === 'developer') this._developerSettings(P, sect);
        if (pg === 'vocabulary') this._vocabularySettings(P);
        if (pg === 'shortcuts') this._shortcutSettings(P);
        if (pg === 'setup') P.add_child(this._setupInfo(m, !!depsCard));
        if ((!pg || pg === 'setup') && m.last_error) {
            const e = m.last_error;
            P.add_child(wrapLbl(lbl('Last problem' + (e.time ? ' · ' + e.time : '') + ': ' + e.summary, 10, '#ffb84d', 'bold')));
            if (e.detail) P.add_child(wrapLbl(lbl(e.detail, 9.5, SEC)));
            P.add_child(this._pill('Dismiss message', 'plain', () => this._cli(['--apply', JSON.stringify({clear_error: true})])));
        }

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
        this._updatePanelStatus(); this._ensureSpin(); this._panelTheme();
        if (this._panel.visible) { this._placePanel(); this._geomSoon(); }
    }

    _settingsScroll(child, reserved = 230) {
        const mon = Main.layoutManager.primaryMonitor;
        const scroll = new St.ScrollView({style: `max-height: ${Math.max(160, Math.min(430, (mon?.height || 720) - reserved))}px;`,
            overlay_scrollbars: true, hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC, x_expand: true});
        scroll.add_child(child); this._listScroll = scroll; this._restoreScroll(); return scroll;
    }
    _dropdown(id, options, selected, changed, disabled = false) {
        const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 4px;'});
        const current = options.find(([value]) => value === selected);
        const button = this._pill((current?.[1] || 'Select…') + (this._dropOpen === id ? ' ▴' : ' ▾'), 'plain', () => {
            this._dropOpen = this._dropOpen === id ? null : id; this._buildPanel();
        }, !disabled);
        box.add_child(button);
        if (this._dropOpen === id) {
            const list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true,
                style: 'padding: 5px; spacing: 3px; background-color: rgba(0,0,0,0.30); border-radius: 6px;'});
            for (const [value, title] of options) list.add_child(this._pill((selected === value ? '✓ ' : '') + title, selected === value ? 'primary' : 'plain', () => {
                changed(value); this._dropOpen = null; this._saveError = ''; this._buildPanel();
            }));
            const scroll = new St.ScrollView({style: 'max-height: 160px;', hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC, x_expand: true});
            scroll.add_child(list); box.add_child(scroll);
        }
        return box;
    }
    _prefChoice(P, sect, key, title, options) {
        P.add_child(sect(title, true));
        P.add_child(this._dropdown(key, options, this._prefDraft[key], value => { this._prefDraft[key] = value; }));
    }
    _prefToggle(P, key, title) {
        const on = this._prefDraft[key];
        P.add_child(this._pill(`${on ? '✓' : '○'} ${title}`, on ? 'primary' : 'plain', () => {
            this._prefDraft[key] = !on; this._saveError = ''; this._buildPanel();
        }));
    }
    _appearanceSettings(P, sect) {
        this._prefChoice(P, sect, 'bar_theme', 'Dictation bubble theme', THEME_OPTIONS);
        this._prefChoice(P, sect, 'settings_theme', 'Settings theme', THEME_OPTIONS);
        this._prefChoice(P, sect, 'bar_size', 'Size', [['tiny', 'Tiny · half width'], ['compact', 'Compact'], ['comfortable', 'Comfortable'], ['large', 'Large']]);
        P.add_child(sect('Background opacity · 0–100%'));
        const entry = new St.Entry({text: String(this._prefDraft.bar_opacity), can_focus: true, hint_text: '0–100', style: 'padding: 7px;'});
        entry.clutter_text.connect('key-focus-in', () => this._beginEditing(entry.clutter_text));
        entry.clutter_text.connect('text-changed', () => {
            const value = entry.get_text().trim();
            if (!/^\d{1,3}$/.test(value) || Number(value) > 100) { this._opacityInvalid = true; this._saveError = 'Enter a whole percentage from 0 to 100.'; this._updatePanelStatus(); return; }
            this._opacityInvalid = false; this._prefDraft.bar_opacity = Number(value); this._saveError = ''; this._updatePanelStatus();
        });
        entry.clutter_text.connect('activate', () => { if (this._opacityInvalid) return; this._endEditing(); this._buildPanel(); });
        P.add_child(entry);
        this._prefToggle(P, 'show_model', 'Show model name (hidden in Tiny)');
    }
    _panelTheme() {
        const theme = this._models?.settings?.settings_theme || 'dark';
        const rgb = THEMES[theme] || THEMES.dark;
        this._panel.style = `background-color: rgb(${rgb}); border: 1px solid rgba(128,128,128,0.4); border-radius: 12px; padding: 14px 16px 16px; spacing: 4px; ${FONT}`;
        if (theme !== 'light') return;
        for (const child of this._panel.get_children()) this._lightenPanelActor(child);
    }
    _lightenPanelActor(actor) {
        if (actor.style) actor.style = actor.style.replace(/rgba\(255,\s*255,\s*255,/g, 'rgba(0,0,0,').replace(/#fff(?:fff)?\b/gi, '#171717').replace(/color: #4cc2ff/g, 'color: #006ca0').replace(/color: #ff8a8d/g, 'color: #a51926').replace(/color: #ff9a9c/g, 'color: #a51926');
        for (const child of actor.get_children()) this._lightenPanelActor(child);
    }
    _developerSettings(host, sect) {
        const P = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 8px;'});
        host.add_child(this._settingsScroll(P));
        P.add_child(sect('Session diagnostics'));
        this._prefToggle(P, 'developer_logging', 'Enable developer logging');
        P.add_child(wrapLbl(lbl('Off by default. Click Save after changing this setting. When enabled, logs include recognized speech, typed text, settings, microphone diagnostics, setup activity and errors. No raw audio is saved and nothing is uploaded.', 9.5, SEC)));
        P.add_child(wrapLbl(lbl('One readable .txt file per session. A new file starts when logging is enabled, the daemon starts, or dictation opens from closed. Pause/resume stays in the same file. Turning logging off preserves existing files.', 9.5, SEC)));
        const folder = GLib.get_user_state_dir() + '/gnome-voice-typing/logs';
        const path = wrapLbl(lbl(folder, 9.5, SEC)); path.clutter_text.selectable = true; P.add_child(path);
        P.add_child(this._pill('Copy latest log', 'primary', () => this._logAction('text')));
        P.add_child(this._pill('Open latest log', 'plain', () => this._logAction('latest')));
        P.add_child(this._pill('Open log folder', 'plain', () => this._logAction('folder')));
        P.add_child(wrapLbl(lbl('Review speech and personal details before sharing a log. Logs remain until you delete them from the folder.', 9.5, SEC)));
        if (this._models?.logging_error) P.add_child(wrapLbl(lbl('Could not write the log: ' + this._models.logging_error, 9.5, '#ff9a9c')));
        if (this._devMessage) P.add_child(wrapLbl(lbl(this._devMessage, 9.5, SEC)));
    }
    _logAction(action) {
        const tool = this._toolPath('voice-typing-logs'); if (!tool) return;
        try {
            const p = Gio.Subprocess.new([tool, action], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            p.communicate_utf8_async(null, null, (pr, result) => {
                try {
                    const [, out, err] = pr.communicate_utf8_finish(result);
                    if (!this._panel) return;
                    if (!pr.get_successful()) throw new Error(err.trim() || 'Could not read the log.');
                    if (action === 'text') {
                        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, out);
                        this._devMessage = 'Latest session copied. Review it before sharing.';
                    } else {
                        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(out.trim()).get_uri(), global.create_app_launch_context(0, -1));
                        this._devMessage = action === 'folder' ? 'Log folder opened.' : 'Latest session opened.';
                    }
                } catch (error) { this._devMessage = String(error.message || error); }
                if (this._panelOpen && this._page === 'developer') this._buildPanel();
            });
        } catch (error) { this._devMessage = String(error.message || error); this._buildPanel(); }
    }
    _formattingSettings(P, sect) {
        this._prefChoice(P, sect, 'punctuation', 'Automatic punctuation', [['model', 'Keep model punctuation'], ['none', 'Remove punctuation']]);
        this._prefChoice(P, sect, 'capitalization', 'Capitalization', [['model', 'Keep model capitalization'], ['sentence', 'Capitalize sentence starts'], ['lower', 'Lowercase']]);
        this._prefToggle(P, 'spoken_punctuation', 'Convert spoken punctuation');
        this._prefToggle(P, 'trailing_space', 'Add a space after each phrase');
        P.add_child(wrapLbl(lbl('Say “comma”, “period”, “question mark”, “exclamation mark”, “colon” or “semicolon”. Custom formatting waits until the phrase ends.', 9.5, SEC)));
    }
    _beginEditing(actor) {
        if (!this._editGrab) {
            this._cli(['pause']);
            try { this._editGrab = Main.pushModal(this._panel, {actionMode: Shell.ActionMode.POPUP}); }
            catch (e) { this._saveError = 'Could not focus the editor. Close Settings and try again.'; return; }
        }
        this._editing = true; this._panel.can_focus = true;
        actor.grab_key_focus();
    }
    _endEditing() {
        this._editing = false;
        if (this._panel) this._panel.can_focus = false;
        if (this._shortcutCapture) { this._panel.disconnect(this._shortcutCapture); this._shortcutCapture = 0; }
        if (this._editGrab) { try { Main.popModal(this._editGrab); } catch (e) {} this._editGrab = null; }
    }
    _vocabularySettings(P) {
        P.add_child(wrapLbl(lbl('Heard phrase → preferred text. Matches whole words, ignoring case. Your preferred spelling is kept. Dictation pauses while you type here.', 9.5, SEC)));
        const list = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true, style: 'spacing: 8px;'});
        this._prefDraft.vocabulary.forEach((rule, index) => {
            const row = new St.BoxLayout({style: 'spacing: 5px;', x_expand: true});
            for (const [key, hint] of [['heard', 'Heard phrase'], ['write', 'Write instead']]) {
                const entry = new St.Entry({text: rule[key], hint_text: hint, can_focus: true, x_expand: true, style: 'width: 150px; padding: 7px; color: #fff; background-color: #222; border: 1px solid #666; border-radius: 5px;'});
                entry.clutter_text.max_length = 200;
                entry.clutter_text.connect('key-focus-in', () => { if (!this._editing) this._beginEditing(entry.clutter_text); });
                entry.clutter_text.connect('text-changed', () => { rule[key] = entry.get_text(); this._saveError = ''; this._refreshSave(); });
                entry.clutter_text.connect('key-press-event', (actor, event) => {
                    if (event.get_key_symbol() !== Clutter.KEY_Escape) return Clutter.EVENT_PROPAGATE;
                    this._endEditing(); this._buildPanel(); return Clutter.EVENT_STOP;
                });
                row.add_child(entry);
            }
            row.add_child(this._iconButton('edit-delete-symbolic', 14, true, () => { this._endEditing(); this._prefDraft.vocabulary.splice(index, 1); this._buildPanel(); }));
            list.add_child(row);
        });
        P.add_child(this._settingsScroll(list, 350));
        P.add_child(this._pill('Add word or phrase', 'plain', () => { this._endEditing(); this._prefDraft.vocabulary.push({heard: '', write: ''}); this._buildPanel(); }, this._prefDraft.vocabulary.length < 200));
        P.add_child(this._pill('Done editing', 'plain', () => { this._endEditing(); this._buildPanel(); }));
    }
    _shortcutRequest(bindings = null) {
        const cli = this._models?.cli; if (!cli || this._shortcutPending) return;
        this._shortcutPending = true;
        const args = ['/usr/bin/python3', GLib.path_get_dirname(cli) + '/shortcuts.py'];
        if (bindings) args.push(JSON.stringify(bindings));
        let process;
        try { process = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE); }
        catch (e) { this._shortcutPending = false; this._shortcutError = 'Could not open shortcut settings.'; return; }
        process.communicate_utf8_async(null, null, (p, result) => {
            this._shortcutPending = false;
            try {
                const [, out] = p.communicate_utf8_finish(result); const reply = JSON.parse(out);
                if (reply.error || !p.get_successful()) throw new Error(reply.error || 'Shortcut settings could not be saved.');
                this._bindings = reply.bindings; this._shortcutError = bindings ? 'Shortcuts saved.' : '';
            } catch (e) { this._shortcutError = e.message; }
            if (this._bar && this._panelOpen && this._page === 'shortcuts') this._buildPanel();
        });
    }
    _shortcutSettings(P) {
        P.add_child(wrapLbl(lbl('Click a shortcut and press a combination with Control, Alt or Super. Escape cancels. Conflicting shortcuts will not be changed.', 9.5, SEC)));
        if (!this._bindings) {
            P.add_child(lbl(this._shortcutError || 'Loading shortcuts…', 10, SEC));
            if (!this._shortcutPending && !this._shortcutError) this._shortcutRequest();
            return;
        }
        for (const [action, title] of [['toggle', 'Start / pause dictation'], ['close', 'Close dictation']]) {
            P.add_child(lbl(title, 10.5, '#fff', 'bold'));
            const row = new St.BoxLayout({style: 'spacing: 8px;'});
            const display = (this._bindings[action] || 'Disabled').replace(/></g, '+').replace(/[<>]/g, '');
            const button = this._pill(display, 'plain', () => {
                this._beginEditing(this._panel);
                if (!this._editing) return;
                button.get_child().text = 'Press shortcut…';
                this._shortcutCapture = this._panel.connect('key-press-event', (actor, event) => {
                    const key = event.get_key_symbol(), mods = event.get_state();
                    if (key === Clutter.KEY_Escape) { this._endEditing(); this._buildPanel(); return Clutter.EVENT_STOP; }
                    const control = !!(mods & Clutter.ModifierType.CONTROL_MASK), alt = !!(mods & Clutter.ModifierType.MOD1_MASK), superKey = !!(mods & Clutter.ModifierType.SUPER_MASK);
                    if (!control && !alt && !superKey) return Clutter.EVENT_STOP;
                    if ([Clutter.KEY_Control_L, Clutter.KEY_Control_R, Clutter.KEY_Alt_L, Clutter.KEY_Alt_R, Clutter.KEY_Shift_L, Clutter.KEY_Shift_R, Clutter.KEY_Super_L, Clutter.KEY_Super_R].includes(key)) return Clutter.EVENT_STOP;
                    const mask = mods & (Clutter.ModifierType.CONTROL_MASK | Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.SUPER_MASK | Clutter.ModifierType.SHIFT_MASK);
                    const value = Meta.accelerator_name(mask, key);
                    this._bindings[action] = value; this._shortcutError = ''; this._endEditing(); this._buildPanel(); return Clutter.EVENT_STOP;
                });
            }, !this._shortcutPending);
            row.add_child(button);
            row.add_child(this._pill('Disable', 'plain', () => { this._bindings[action] = ''; this._shortcutError = ''; this._buildPanel(); }, !this._shortcutPending));
            P.add_child(row);
        }
        P.add_child(this._pill('Save shortcuts', 'primary', () => this._shortcutRequest(this._bindings), !this._shortcutPending));
        if (this._shortcutError) P.add_child(wrapLbl(lbl(this._shortcutError, 9.5, SEC)));
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
                typing: 'Pause after ' + (pause === 'Never' ? 'never' : pause) + '  ·  sounds ' + (this._sel.sounds ? 'on' : 'off') + '  ·  follow ' + (this._sel.follow ? 'on' : 'off'),
                appearance: 'Theme · size · opacity · model label',
                formatting: 'Punctuation · capitalization · spacing',
                vocabulary: 'Preferred words and spellings',
                shortcuts: 'Start/pause and close dictation',
                developer: this._models.settings.developer_logging ? 'Session logging on · contains speech' : 'Session logging off · copy and open logs',
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
                }, () => { if (id === 'shortcuts') { this._bindings = null; this._shortcutError = ''; } this._dropOpen = null; this._page = id; this._scrollKeep = 0; this._unConfirm = false; this._buildPanel(); });
                box.add_child(row); this._catActors[id] = row;
            }
        } catch (e) { console.error('voice-typing settings home: ' + e); }
        return this._settingsScroll(box, 260);
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
    _noiseTick() { if (this._panelOpen && this._page === 'mic' && (!this._info || this._info.state !== 'listening')) this._cli(['meter']); }
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
        if (this._saveError) { this._statusLabel.text = this._saveError; this._statusLabel.style = sty('#ff8a8d', 10); }
        else if (this._saveState === 'saving') { this._statusLabel.text = 'Saving…'; this._statusLabel.style = sty(SEC, 10); }
        else if (loading) { this._statusLabel.text = `Loading ${i.loading || sw.target}…`; this._statusLabel.style = sty(ACCENT, 10); }
        else if (sw.status === 'error') { this._statusLabel.text = sw.msg; this._statusLabel.style = sty('#ff8a8d', 10); }
        else if (sw.status === 'ready' && GLib.get_monotonic_time() < this._readyUntil) { this._statusLabel.text = '✓ Ready'; this._statusLabel.style = sty('#7ddc70', 10.5, true); }
        else { this._statusLabel.text = ''; }
        this._ensureSpin(); this._refreshSave();
    }

    _save() {
        if (this._opacityInvalid || this._isLoading() || this._saveState !== 'idle' || !this._dirty()) return;
        this._endEditing();
        const m = this._models, payload = {};
        if (this._sel.model !== m.current) { payload.model = this._sel.model; payload.device = devOf(this._entries().find(x => x.id === payload.model)); }
        if (this._sel.pause !== m.settings.auto_pause_seconds) payload.auto_pause_seconds = this._sel.pause;
        if (this._sel.mic !== (m.settings.mic || '')) payload.mic = this._sel.mic;
        if (this._sel.sounds !== (m.settings.sounds !== false)) payload.sounds = this._sel.sounds;
        if (this._sel.follow !== (m.settings.follow_field === true)) payload.follow_field = this._sel.follow;
        const submittedDraft = clone(this._prefDraft), sentPrefs = clone(this._prefDraft), sentSel = clone(this._sel);
        sentPrefs.vocabulary = sentPrefs.vocabulary.map(rule => ({heard: rule.heard.trim(), write: rule.write.trim()})).filter(rule => rule.heard || rule.write);
        for (const key of Object.keys(PREFS)) if (JSON.stringify(sentPrefs[key]) !== JSON.stringify(this._basePrefs[key])) payload[key] = sentPrefs[key];
        this._saveError = ''; this._saveState = 'saving'; this._buildPanel();
        let process;
        try { process = Gio.Subprocess.new([m.cli, '--apply', JSON.stringify(payload)], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE); }
        catch (e) { this._saveState = 'idle'; this._saveError = 'Could not contact the background service.'; this._buildPanel(); return; }
        process.communicate_utf8_async(null, null, (p, result) => {
            if (!this._bar) return;
            try {
                const [, out] = p.communicate_utf8_finish(result); const reply = JSON.parse(out);
                if (!p.get_successful() || !reply.ok) throw new Error(reply.error || 'The service did not confirm your settings.');
                if (!reply.preferences && Object.keys(payload).some(key => key in PREFS)) throw new Error('Log out and back in to load the updated service before saving these settings.');
                if (reply.preferences && Object.keys(payload).some(key => key in PREFS && JSON.stringify(reply.preferences[key]) !== JSON.stringify(sentPrefs[key]))) throw new Error('Settings were not confirmed. Please try again.');
                if (JSON.stringify(this._prefDraft) === JSON.stringify(submittedDraft)) this._prefDraft = clone(sentPrefs);
                this._base = sentSel; this._basePrefs = sentPrefs; this._saveState = 'saved';
                if (this._saveTimer) GLib.source_remove(this._saveTimer);
                this._saveTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1400, () => { this._saveTimer = 0; this._saveState = 'idle'; if (this._panelOpen) this._updatePanelStatus(); return GLib.SOURCE_REMOVE; });
            } catch (e) { this._saveState = 'idle'; this._saveError = e.message; }
            if (this._panelOpen) this._buildPanel();
        });
    }
}
