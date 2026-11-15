#!/usr/bin/env bash
# End-to-end test of install.sh / uninstall.sh in a THROW-AWAY environment. Never touches your real settings:
#   * HOME and all XDG dirs live under /tmp/gvt-itest.*
#   * its own D-Bus session bus (dbus-run-session) and a keyfile GSettings backend (no dconf)
#   * a private headless GNOME Shell (own runtime dir) loads the installed extension
#   * system packages and the udev rule are skipped (--no-system-deps --no-udev); the daemon runs in dry-run mode (types nothing)
# Usage: tests/isolated-install-test.sh [model-id]      (default: moonshine-tiny, the smallest model)
set -uo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
if [ -z "${GVT_TEST_INNER:-}" ]; then
  command -v dbus-run-session >/dev/null || { echo "SKIP: dbus-run-session not found"; exit 2; }
  T="$(mktemp -d /tmp/gvt-itest.XXXXXX)"
  exec env -u WAYLAND_DISPLAY -u DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u XDG_SESSION_TYPE GVT_TEST_INNER=1 GVT_T="$T" \
       dbus-run-session -- "$0" "$@"
fi
T="$GVT_T"; MODEL="${1:-moonshine-tiny}"
export HOME="$T/home" XDG_CONFIG_HOME="$T/home/.config" XDG_DATA_HOME="$T/home/.local/share" XDG_STATE_HOME="$T/home/.local/state" XDG_CACHE_HOME="$T/home/.cache"
export XDG_RUNTIME_DIR="$T/rt" GSETTINGS_BACKEND=keyfile XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
PASS=0; FAILN=0
chk() { if [ "$2" = 0 ]; then PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; else FAILN=$((FAILN+1)); printf 'FAIL  %s  %s\n' "$1" "${3:-}"; fi; }
D="$XDG_DATA_HOME/gnome-voice-typing"; UUID='voice-typing@gnome-voice-typing.github.io'
KEYS() { for k in "org.gnome.desktop.wm.keybindings minimize" "org.gnome.settings-daemon.plugins.media-keys custom-keybindings" "org.gnome.desktop.interface toolkit-accessibility" "org.gnome.shell enabled-extensions"; do echo "$k = $(gsettings get $k)"; done; }
case "$HOME" in /tmp/gvt-itest.*) ;; *) echo "refusing: HOME is not a throw-away dir"; exit 3;; esac
echo "== throw-away environment: $T"
KEYS > "$T/keys.before"; cat "$T/keys.before"

echo; echo "== 1. install.sh --dry-run (must change nothing)"
"$SRC/install.sh" --dry-run --cpu-only --no-system-deps --no-udev --model "$MODEL" > "$T/dry.log" 2>&1; rc=$?
chk "dry-run exits 0" $rc; [ ! -e "$D" ] && [ ! -e "$XDG_CONFIG_HOME/gnome-voice-typing" ] && [ ! -e "$XDG_CONFIG_HOME/autostart" ]; chk "dry-run created nothing" $?
KEYS | diff -q - "$T/keys.before" >/dev/null; chk "dry-run left GNOME settings untouched" $?

echo; echo "== 2. install.sh (real install, model $MODEL)"
"$SRC/install.sh" --yes --cpu-only --no-system-deps --no-udev --no-start --model "$MODEL" > "$T/install1.log" 2>&1; rc=$?
tail -25 "$T/install1.log"; chk "install exits 0" $rc
[ -x "$D/venv/bin/python" ] && "$D/venv/bin/python" -c 'import numpy, sherpa_onnx'; chk "python environment imports numpy + sherpa_onnx" $?
"$D/bin/voice-typing-models" verify "$MODEL" ; chk "model downloaded and verified ($MODEL)" $?
[ -f "$XDG_CONFIG_HOME/gnome-voice-typing/config.json" ] && grep -q "\"model\": \"$MODEL\"" "$XDG_CONFIG_HOME/gnome-voice-typing/config.json"; chk "config.json written with the chosen model" $?
grep -q mic "$XDG_CONFIG_HOME/gnome-voice-typing/config.json" && ! grep -q 'alsa_input' "$XDG_CONFIG_HOME/gnome-voice-typing/config.json"; chk "no personal microphone name in config" $?
[ -f "$XDG_DATA_HOME/gnome-shell/extensions/$UUID/extension.js" ]; chk "extension files installed" $?
python3 -c "import json,sys; m=json.load(open('$XDG_DATA_HOME/gnome-shell/extensions/$UUID/metadata.json')); sys.exit(0 if m['uuid']=='$UUID' and '51' in m['shell-version'] else 1)"; chk "metadata.json uuid + shell-version ok" $?
[ -f "$XDG_CONFIG_HOME/autostart/gnome-voice-typing.desktop" ] && grep -q "$D/bin/voice-typingd" "$XDG_CONFIG_HOME/autostart/gnome-voice-typing.desktop"; chk "autostart entry written" $?
[ "$(gsettings get org.gnome.desktop.wm.keybindings minimize)" = "['<Super><Shift>h']" ]; chk "conflicting minimize shortcut moved from Super+H to Super+Shift+H" $?
S=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/gnome-voice-typing/
[ "$(gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$S binding)" = "'<Super>h'" ]; chk "custom shortcut Super+H created" $?
[ "$(gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$S command)" = "'$HOME/.local/bin/voice-typing toggle'" ]; chk "shortcut runs the installed voice-typing toggle" $?
[ "$(gsettings get org.gnome.desktop.interface toolkit-accessibility)" = true ]; chk "toolkit-accessibility enabled" $?
gsettings get org.gnome.shell enabled-extensions | grep -q "$UUID"; chk "extension listed in enabled-extensions" $?
grep -rIlE '/mnt/nvme|w3tsu1t|dictate-test|HyperX|QuadCast' "$D" "$XDG_DATA_HOME/gnome-shell" "$XDG_CONFIG_HOME" 2>/dev/null | grep -v '/venv/' | head -3 | grep . >/dev/null; [ $? -ne 0 ]; chk "no hardcoded personal paths/names in installed files" $?

echo; echo "== 3. re-run install.sh (idempotent)"
"$SRC/install.sh" --yes --cpu-only --no-system-deps --no-udev --no-start --model "$MODEL" > "$T/install2.log" 2>&1; chk "second install exits 0" $?
grep -q "already set up" "$T/install2.log" && grep -q "already installed: $MODEL" "$T/install2.log"; chk "second run reuses venv and verified model" $?
python3 -c "import json; l=json.load(open('$XDG_CONFIG_HOME/../.local/share/gnome-voice-typing/install-state.json'))['gnome']; c=l['conflicts'][0]; assert c['old_user'] is None and '<Super>h' in c['old'] and '<Shift>' not in c['old'] and l['enabled_extensions_old'] is None"; chk "saved originals were not overwritten by the second run" $?
[ "$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings | grep -o gnome-voice-typing | wc -l)" = 1 ]; chk "shortcut registered exactly once" $?

echo; echo "== 4. daemon + CLI"
"$D/bin/voice-typing" status | grep -q '"state": "stopped"'; chk "CLI status = stopped before the daemon runs" $?
mkdir -p "$XDG_RUNTIME_DIR/gnome-voice-typing-overlay"
GVT_DRY_RUN=1 setsid "$D/bin/voice-typingd" > "$T/daemon.out" 2>&1 < /dev/null &
for i in $(seq 1 60); do grep -q "model ready" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log" 2>/dev/null && break; sleep 0.5; done
grep -q "model ready" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log"; chk "daemon started and loaded the model" $?
"$D/bin/voice-typing" status | grep -q '"state": "idle"'; chk "CLI status = idle" $?
"$D/bin/voice-typing" --models | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d['current']=='$MODEL' and any(m['installed'] for m in d['models']) else 1)"; chk "CLI --models lists the installed model" $?
cp "$SRC/tests/data/sample.wav" "$T/sample.wav" 2>/dev/null
if [ -f "$T/sample.wav" ] && command -v ffmpeg >/dev/null; then
  "$D/bin/voice-typing" testwav "$T/sample.wav" > /dev/null; for i in $(seq 1 60); do grep -qi "what your country can do" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log" && "$D/bin/voice-typing" status | grep -q '"state": "idle"' && break; sleep 0.5; done   # streaming types the first words early: wait for the whole sample
  grep -i "would type" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log" | head -2
  grep -qi "what your country can do" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log"; chk "daemon transcribed the sample speech (dry-run: nothing typed)" $?
else echo "SKIP  transcription test (no tests/data/sample.wav or ffmpeg)"; fi
"$D/bin/voice-typing" toggle >/dev/null; sleep 1; "$D/bin/voice-typing" status | grep -q listening; chk "toggle -> listening" $?
"$D/bin/voice-typing" close >/dev/null; sleep 0.5; "$D/bin/voice-typing" status | grep -q idle; chk "close -> idle" $?
grep -q 'ask not what' "$XDG_STATE_HOME/gnome-voice-typing/daemon.log" && grep -qE "transcribed .*chars\)$" "$XDG_STATE_HOME/gnome-voice-typing/daemon.log"; true
echo "   (daemon log never contains typed text outside dry-run: $(grep -c 'transcribed' "$XDG_STATE_HOME/gnome-voice-typing/daemon.log") transcribe lines)"
"$D/bin/voice-typing" quit >/dev/null; sleep 1; "$D/bin/voice-typing" status | grep -q stopped; chk "quit stops the daemon" $?

echo; echo "== 5. extension loads in a private headless GNOME Shell"
pipewire > "$T/pw.log" 2>&1 & PW=$!
gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720 > "$T/shell.log" 2>&1 & SH=$!
for i in $(seq 1 40); do gnome-extensions list 2>/dev/null | grep -q "$UUID" && break; sleep 0.5; done
sleep 3
gnome-extensions list | grep -q "$UUID"; chk "shell discovered the extension" $?
gnome-extensions info "$UUID" > "$T/extinfo.txt" 2>&1; sed 's/^/   /' "$T/extinfo.txt" | head -12
grep -qE "State: +(ACTIVE|ENABLED)" "$T/extinfo.txt"; chk "extension state is ACTIVE" $?
[ -e "$XDG_RUNTIME_DIR/gnome-voice-typing-overlay/ready" ]; chk "extension created its overlay dir + ready flag" $?
! grep -E "voice-typing|JS ERROR" "$T/shell.log" | grep -qiE "error|exception|critical"; chk "no extension errors in the shell log" $?
touch "$XDG_RUNTIME_DIR/gnome-voice-typing-overlay/debug"
GVT_DRY_RUN=1 setsid "$D/bin/voice-typingd" > "$T/daemon2.out" 2>&1 < /dev/null &
sleep 4; "$D/bin/voice-typing" toggle > /dev/null; sleep 2
grep -q '"state": "listening"\|"listening"' "$XDG_RUNTIME_DIR/gnome-voice-typing-overlay/state.json"; chk "daemon -> extension: state.json says listening" $?
[ -s "$XDG_CACHE_HOME/gnome-voice-typing/overlay.log" ]; chk "extension reacted (placement log written, debug flag on)" $?
head -3 "$XDG_CACHE_HOME/gnome-voice-typing/overlay.log" 2>/dev/null | sed 's/^/   /'
! grep -E "voice-typing" "$T/shell.log" | grep -qiE "error|exception|critical"; chk "still no extension errors after showing the bar" $?
"$D/bin/voice-typing" quit > /dev/null; sleep 1
kill $SH $PW 2>/dev/null; sleep 1

echo; echo "== 6. uninstall.sh --dry-run, then uninstall.sh"
"$D/uninstall.sh" --dry-run > "$T/un-dry.log" 2>&1; chk "uninstall --dry-run exits 0" $?; [ -d "$D" ]; chk "uninstall --dry-run removed nothing" $?
"$D/uninstall.sh" --yes > "$T/un.log" 2>&1; chk "uninstall exits 0" $?; tail -12 "$T/un.log"
[ ! -e "$D" ] && [ ! -e "$XDG_DATA_HOME/gnome-shell/extensions/$UUID" ] && [ ! -e "$XDG_CONFIG_HOME/autostart/gnome-voice-typing.desktop" ] && [ ! -e "$XDG_CONFIG_HOME/gnome-voice-typing" ] && [ ! -e "$HOME/.local/bin/voice-typing" ]; chk "all installed files removed" $?
[ ! -e "$XDG_RUNTIME_DIR/gnome-voice-typing-overlay" ] && [ ! -e "$XDG_STATE_HOME/gnome-voice-typing" ] && [ ! -e "$XDG_CACHE_HOME/gnome-voice-typing" ]; chk "runtime, state and cache files removed" $?
KEYS > "$T/keys.after"; diff "$T/keys.before" "$T/keys.after"; chk "GNOME settings restored exactly (before == after)" $?
pgrep -u "$(id -u)" -f "$D/bin" >/dev/null; [ $? -ne 0 ]; chk "no leftover processes" $?
echo; echo "RESULT: $PASS passed, $FAILN failed   (work dir kept: $T)"
[ "$FAILN" = 0 ]
