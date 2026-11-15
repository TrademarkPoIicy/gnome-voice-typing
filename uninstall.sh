#!/usr/bin/env bash
# gnome-voice-typing uninstaller: restores every GNOME setting that install.sh changed and removes everything it added.
#   ./uninstall.sh --dry-run   show what would be removed/restored
#   ./uninstall.sh             do it (asks before the sudo step)
#   options: -y|--yes  --keep-config (keep ~/.config/gnome-voice-typing)  --keep-models (keep downloaded models, ~1-2 GB)
#            --pkexec  ask for the root step with ONE graphical (polkit) prompt instead of sudo; used automatically when there is no terminal
#                      (e.g. started from the settings panel). If that prompt is declined everything else is still removed.
set -uo pipefail
APP=gnome-voice-typing
UUID='voice-typing@gnome-voice-typing.github.io'
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DRY=0; YES=0; KEEP_CONF=0; KEEP_MODELS=0; PKEXEC=0
for a in "$@"; do case "$a" in --dry-run) DRY=1;; -y|--yes) YES=1;; --keep-config) KEEP_CONF=1;; --keep-models) KEEP_MODELS=1;; --pkexec) PKEXEC=1;;
  -h|--help) sed -n 2,6p "$0"; exit 0;; *) echo "unknown option: $a" >&2; exit 2;; esac; done
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; D=$'\e[2m'; Z=$'\e[0m'; else B=; G=; Y=; D=; Z=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$Z"; }
say()  { printf '    %s\n' "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$Z" "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '    %s[dry-run]%s %s\n' "$D" "$Z" "$*"; else "$@"; fi; }
ask()  { if [ "$YES" = 1 ]; then return 0; fi; if [ "$DRY" = 1 ] || [ ! -t 0 ]; then [ "${2:-y}" = y ]; return; fi
         local a; read -r -p "    ? $1 [Y/n] " a; case "${a:-y}" in [Yy]*) return 0;; *) return 1;; esac; }

# where did install.sh put things? (state file wins; defaults otherwise)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"; [ -f "$HERE/install-state.json" ] && DATA="$HERE"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"; STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP"
EXTDIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"; AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop"
LOCALBIN="$HOME/.local/bin"; ST="$DATA/install-state.json"
# run from a temporary copy when started from inside the install dir: the script then survives its own deletion and the extension being removed
if [ "$DRY" = 0 ] && [ -z "${GVT_UNINSTALL_RELOC:-}" ] && [ "$HERE" = "$DATA" ]; then
  TMPU="$(mktemp /tmp/$APP-uninstall.XXXXXX.sh)" && cp "$0" "$TMPU" && chmod +x "$TMPU" && GVT_UNINSTALL_RELOC=1 exec "$TMPU" "$@"
fi
if [ -n "${GVT_UNINSTALL_RELOC:-}" ] && [ ! -t 1 ]; then exec >>"/tmp/$APP-uninstall.log" 2>&1; date; fi    # no terminal (started from the panel): keep a log in /tmp
RT="${XDG_RUNTIME_DIR:-/tmp/$APP-$(id -u)}"
[ "$DRY" = 1 ] && printf '%s*** DRY RUN: nothing will be changed ***%s\n' "$Y" "$Z"
printf '%sUninstalling gnome-voice-typing%s\n' "$B" "$Z"
[ -d "$DATA" ] || warn "$DATA does not exist (already removed?). Cleaning up anything else that is left."

step "1/7 Stopping the background service"
if [ -x "$DATA/bin/voice-typing" ]; then run "$DATA/bin/voice-typing" quit >/dev/null 2>&1; fi
run pkill -u "$(id -u)" -f "$DATA/bin/dictated.py"
run pkill -u "$(id -u)" -f "ydotoold -p $RT/$APP-ydotool.sock"
run pkill -u "$(id -u)" -f "$DATA/bin/inject.py"
run rm -f "$RT/$APP.sock" "$RT/$APP-ydotool.sock"; run rm -rf "$RT/$APP-overlay"
say "done"

step "2/7 Restoring GNOME settings (shortcut, conflicting shortcuts, accessibility, extension list)"
GG="$DATA/lib/gvt_gnome.py"; [ -f "$GG" ] || GG="$HERE/src/lib/gvt_gnome.py"
if [ -f "$GG" ] && [ -f "$ST" ] && command -v python3 >/dev/null; then
  if [ "$DRY" = 1 ]; then python3 "$GG" restore --state "$ST" --dry-run | sed 's/^/    /'; else python3 "$GG" restore --state "$ST" | sed 's/^/    /'; fi
else warn "No install record found: no GNOME settings were recorded, so none are changed."; fi

step "3/7 Removing the extension and the autostart entry"
run rm -rf "$EXTDIR"; run rm -f "$AUTOSTART"
run rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/gnome-voice-typing.desktop"; command -v update-desktop-database >/dev/null && run update-desktop-database "${XDG_DATA_HOME:-$HOME/.local/share}/applications"   # (app-grid launcher, added locally after install)
run rm -f "$LOCALBIN/voice-typing" "$LOCALBIN/voice-typing-models"
say "(a running GNOME Shell keeps the extension in memory until you log out; it is already disabled for the next login)"

step "4/7 Removing the remote-control permission (only if the portal fallback was ever used)"
if [ -f "$CONF/portal_token" ]; then
  T="$(cat "$CONF/portal_token")"
  run busctl --user call org.freedesktop.impl.portal.PermissionStore /org/freedesktop/impl/portal/PermissionStore org.freedesktop.impl.portal.PermissionStore Delete ss remote-desktop "$T"
else say "nothing to remove"; fi

step "5/7 Removing the /dev/uinput udev rule (needs sudo)"
RULE=/etc/udev/rules.d/70-$APP-uinput.rules; MODLOAD=/etc/modules-load.d/$APP-uinput.conf
if [ -e "$RULE" ] || [ -e "$MODLOAD" ]; then
  say "These files were added by the installer: $RULE $MODLOAD"
  ROOTCMD="rm -f $RULE $MODLOAD; udevadm control --reload; udevadm trigger --name-match=uinput --action=change"
  if ask "Remove them now (needs administrator rights)?" y; then
    if [ "$(id -u)" = 0 ]; then run sh -c "$ROOTCMD"
    elif [ "$PKEXEC" = 1 ] || [ ! -t 0 ]; then
      if command -v pkexec >/dev/null; then run pkexec sh -c "$ROOTCMD" || warn "Administrator prompt declined or failed: those two files were left in place (everything else is removed). Remove later with: sudo rm -f $RULE $MODLOAD"
      else warn "pkexec not found. Remove later with: sudo rm -f $RULE $MODLOAD"; fi
    else
      run sudo sh -c "$ROOTCMD" || warn "sudo failed. Remove later with: sudo rm -f $RULE $MODLOAD"
    fi
  else warn "Left in place. Remove later with: sudo rm -f $RULE $MODLOAD"; fi
else say "not installed"; fi

step "6/7 Removing program files, models and caches"
if [ "$KEEP_MODELS" = 1 ] && [ -d "$DATA/models" ]; then
  say "Keeping models in $DATA/models (--keep-models)"
  if [ "$DRY" = 1 ]; then say "[dry-run] would remove everything else in $DATA"; else
    find "$DATA" -mindepth 1 -maxdepth 1 ! -name models -exec rm -rf {} +; fi
else run rm -rf "$DATA"; fi
run rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/$APP"; run rm -rf "$STATE_DIR"
say "done"

step "7/7 Your settings"
if [ "$KEEP_CONF" = 1 ]; then say "Keeping $CONF (--keep-config)"
elif [ -d "$CONF" ]; then
  if ask "Delete your settings in $CONF (model choice, microphone, pause time)?" y; then run rm -rf "$CONF"; else say "Kept $CONF"; fi
else say "nothing to remove"; fi
if [ "$DRY" = 1 ]; then printf '\n    Dry run finished: nothing was changed.\n'; else printf '\n    %sgnome-voice-typing has been removed.%s Packages installed by your package manager (ydotool, wl-clipboard, build tools ...) were left alone; remove them with your package manager if you do not need them.\n' "$G" "$Z"; fi
