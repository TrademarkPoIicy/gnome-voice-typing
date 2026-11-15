#!/usr/bin/env bash
# Reverses install.sh: removes the speaker driver, its services and files, the EasyEffects presets
# (only if you have not edited them) and the hidden autostart entry. Packages are left installed.
#   ./uninstall.sh [--dry-run] [-y]
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

DRY=0; YES=0
for a in "$@"; do case "$a" in
    --dry-run) DRY=1;; -y|--yes) YES=1;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown option: $a" >&2; exit 2;; esac; done
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo."

DKMS_NAME=max98390-hda; DKMS_VER=1.0
run() { if [ "$DRY" = 1 ]; then say "would run: $*"; else "$@"; fi; }

if [ "$DRY" = 0 ] && [ "$YES" != 1 ]; then
    read -rp "Remove the Galaxy Book speaker fix (driver, services, presets, autostart)? [y/N] " a
    case "$a" in y|Y|yes) ;; *) echo "Cancelled."; exit 0;; esac
fi

step "Removing the speaker driver and services (sudo)"
for u in max98390-hda-i2c-setup.service max98390-hda-check-upstream.service; do
    run sudo systemctl disable --now "$u" 2>/dev/null || true
done
run sudo rmmod snd_hda_scodec_max98390_i2c 2>/dev/null || true
run sudo rmmod snd_hda_scodec_max98390 2>/dev/null || true
if command -v dkms >/dev/null 2>&1 && dkms status "$DKMS_NAME/$DKMS_VER" 2>/dev/null | grep -q "$DKMS_NAME"; then
    run sudo dkms remove "$DKMS_NAME/$DKMS_VER" --all
else
    say "no DKMS module $DKMS_NAME/$DKMS_VER registered"
fi
run sudo rm -f /etc/systemd/system/max98390-hda-i2c-setup.service /etc/systemd/system/max98390-hda-check-upstream.service \
    /etc/modules-load.d/max98390-hda.conf /usr/local/sbin/max98390-hda-i2c-setup.sh /usr/local/sbin/max98390-hda-check-upstream.sh
run sudo rm -rf "/usr/src/$DKMS_NAME-$DKMS_VER"
run sudo systemctl daemon-reload

step "Removing EasyEffects presets and autostart (your user)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
for dir in "$DATA_HOME/easyeffects/output" "${XDG_CONFIG_HOME:-$HOME/.config}/easyeffects/output" "$HOME/.var/app/com.github.wwmm.easyeffects/data/easyeffects/output"; do
    [ -d "$dir" ] || continue
    while read -r sum name; do
        f="$dir/$name"; [ -e "$f" ] || continue
        if [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sum" ]; then run rm -f "$f"; say "removed $f"
        else say "kept $f (you changed it)"; fi
    done <<< "$PRESET_SHA256S"
done
if [ -e "$AUTOSTART_FILE" ]; then
    if grep -q '^Exec=easyeffects --service-mode --hide-window$' "$AUTOSTART_FILE" && grep -q 'speaker EQ service' "$AUTOSTART_FILE"; then
        run rm -f "$AUTOSTART_FILE"; say "removed $AUTOSTART_FILE"
    else say "kept $AUTOSTART_FILE (not the one this installer wrote)"; fi
fi

echo
say "Left in place: the packages (dkms, kernel-devel, i2c-tools, easyeffects, calf, lsp-plugins-lv2, ...)."
say "Remove them yourself if you want: sudo dnf remove easyeffects calf lsp-plugins-lv2 dkms i2c-tools"
say "A running EasyEffects keeps running until you quit it or log out. Reboot to fully restore the original audio state."
