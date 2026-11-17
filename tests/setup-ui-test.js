// Run with gjs tests/setup-ui-test.js. Exercise actual extension methods with fake I/O.
const {GLib} = imports.gi;
const [, bytes] = GLib.file_get_contents('src/extension/extension.js');
const source = new TextDecoder().decode(bytes).replace(/^import .*;\n/gm, '')
    .replace('export default class VoiceTypingOverlay', 'class VoiceTypingOverlay');
let pending = [], calls = [];
const Gio = {
    SubprocessFlags: {STDOUT_PIPE: 1, STDERR_SILENCE: 2, STDOUT_SILENCE: 4},
    Subprocess: {new(args) {
        calls.push(args);
        return {
            communicate_utf8_async(a, b, cb) { pending.push(() => cb(this, null)); },
            communicate_utf8_finish() { return [true, JSON.stringify({needed: true, dismissed: false, job: {status: 'done'}})]; },
            wait_check_async(a, cb) { pending.push(() => cb(this, null)); },
            wait_check_finish() { return true; },
        };
    }},
};
const Extension = class {};
const Overlay = eval(source + '\nVoiceTypingOverlay;');
function check(ok, msg) { if (!ok) throw new Error(msg); print('PASS ' + msg); }
const ui = new Overlay();
Object.assign(ui, {_bar: {}, _models: {}, _depsRevision: 0, _depsAckPending: false,
    _panelOpen: false, _deps: {job: {status: 'done'}},
    _toolPath: () => '/bin/true', _buildPanel: () => {},
    _openPanel: () => { throw new Error('Settings opened automatically'); }});
ui._refreshDeps(); pending.shift()();
check(!ui._panelOpen, 'missing dependencies do not open Settings');
ui._refreshDeps(); // Delayed old result must not restore the success message.
ui._depsAcknowledge();
check(calls.at(-1).join(' ') === '/bin/true deps ack', 'OK uses persistent acknowledgement');
const before = ui._deps;
pending.shift()();
check(ui._deps === before, 'stale dependency response is ignored after OK');
ui._refreshDeps();
check(pending.length === 1, 'refresh waits for acknowledgement to finish');
pending.shift()();
check(ui._deps.job === null, 'successful acknowledgement clears the visible message');
check(pending.length === 1, 'fresh dependency check follows persisted acknowledgement');
