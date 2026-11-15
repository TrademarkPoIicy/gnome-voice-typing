#!/usr/bin/env bash
# Galaxy Book5 Pro 360 speaker fix for Fedora (an optional extra of gnome-voice-typing).
# It has nothing to do with dictation: it makes the laptop's built-in speakers work and sound good.
#
#   ./install.sh             check the hardware, then install (asks before using sudo)
#   ./install.sh --dry-run   check the hardware and show what would happen; changes nothing
#   ./install.sh --check     only run the hardware check and exit
#
# On any other computer it prints why it does not apply and exits with status 0 without changing anything.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

DRY=0; YES=0; CHECK_ONLY=0; NO_PACKAGES=0; NO_PRESET_LOAD=0
usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOT'

options:
  --dry-run          show what would be done; nothing is changed or downloaded
  --check            only run the hardware check
  -y, --yes          do not ask before using sudo
  --no-packages      do not install distro packages (you must already have them)
  --no-load-preset   install the preset but do not load it into a running EasyEffects
  -h, --help         this text
EOT
}
while [ $# -gt 0 ]; do case "$1" in
    --dry-run) DRY=1;; --check) CHECK_ONLY=1;; -y|--yes) YES=1;;
    --no-packages) NO_PACKAGES=1;; --no-load-preset) NO_PRESET_LOAD=1;;
    -h|--help) usage; exit 0;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2;; esac; shift; done

# ---------------------------------------------------------------- 1. hardware check
step "Checking this computer"
if ! hw_check; then
    echo
    echo "This extra does not apply here: $HW_REASON."
    echo "Nothing was installed or changed."
    exit 0
fi
ok "$HW_REASON"
[ "$CHECK_ONLY" = 1 ] && exit 0

# ---------------------------------------------------------------- 2. other requirements
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo (it asks for sudo itself when needed; the EasyEffects part belongs in your home folder)."
OSID="$(. /etc/os-release 2>/dev/null; echo "${ID:-}")"
OSLIKE="$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}")"
case " $OSID $OSLIKE " in *" fedora "*) ;; *)
    echo "This installer is written for Fedora (found: ${OSID:-unknown}). Nothing was changed."
    echo "Other distributions: use the upstream project directly, see README.md in this folder."
    exit 1;; esac
command -v dnf >/dev/null 2>&1 || die "dnf not found."
command -v curl >/dev/null 2>&1 || die "curl not found (sudo dnf install curl)."

SB="$(mokutil --sb-state 2>/dev/null || true)"
case "$SB" in *"SecureBoot enabled"*) SECURE=1;; *) SECURE=0;; esac

if [ "$DRY" = 1 ]; then
    step "Dry run - this is what would happen (nothing is changed)"
    say "1. sudo dnf install: dkms kernel-devel-$(uname -r) kernel-devel-matched i2c-tools gcc make alsa-sof-firmware easyeffects calf lsp-plugins-lv2"
    say "2. download $UPSTREAM_REPO at commit ${UPSTREAM_COMMIT:0:12}, verify its speaker-fix/ folder against sha256 ${UPSTREAM_TREE_SHA256:0:16}..."
    say "3. sudo: run upstream's speaker-fix/install.sh (DKMS module max98390-hda/1.0, modules-load.d file, two systemd units, helper scripts in /usr/local/sbin)"
    say "4. install the EasyEffects presets into ~/.local/share/easyeffects/output (never overwriting edited ones)"
    say "5. write $AUTOSTART_FILE  (easyeffects --service-mode --hide-window)"
    say "6. start EasyEffects hidden and load $PRESET_NAME"
    [ "$SECURE" = 1 ] && say "Secure Boot is ON: you would be asked for a one-time MOK password and must enroll the key at the next boot."
    exit 0
fi

if [ "$YES" != 1 ]; then
    echo
    echo "This will use sudo to install packages, build a kernel driver (DKMS) and add two systemd services."
    echo "It will also set up EasyEffects for your user. Everything is undone by ./uninstall.sh."
    read -rp "Continue? [y/N] " a; case "$a" in y|Y|yes) ;; *) echo "Cancelled. Nothing was changed."; exit 0;; esac
fi
sudo -v || die "sudo is required."

if [ "$SECURE" = 1 ]; then
    echo
    warn "Secure Boot is ON. The driver is signed with a local key, which must be enrolled once:"
    warn "you will be asked to choose a one-time password now, then reboot and enter it in the blue MOK Manager screen."
    warn "Until that is done the speaker modules will be refused at boot."
fi

# ---------------------------------------------------------------- 3. packages
if [ "$NO_PACKAGES" = 0 ]; then
    step "Installing packages"
    sudo dnf install -y dkms "kernel-devel-$(uname -r)" kernel-devel-matched i2c-tools gcc make alsa-sof-firmware easyeffects calf lsp-plugins-lv2 \
        || die "package install failed. If kernel-devel-$(uname -r) is not available, run 'sudo dnf upgrade --refresh' and reboot into the newest kernel, then try again."
    ok "packages installed"
fi

# ---------------------------------------------------------------- 4. fetch upstream, pinned and verified
step "Fetching $UPSTREAM_REPO at the pinned commit ${UPSTREAM_COMMIT:0:12}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
curl -fsSL --retry 3 -o "$WORK/up.tar.gz" "$UPSTREAM_URL" || die "download failed: $UPSTREAM_URL"
tar -xzf "$WORK/up.tar.gz" -C "$WORK" || die "could not unpack the download"
UP="$WORK/samsung-galaxy-book-linux-fixes-$UPSTREAM_COMMIT/speaker-fix"
[ -d "$UP" ] || die "unexpected layout in the upstream download"
GOT="$(tree_hash "$UP")"
[ "$GOT" = "$UPSTREAM_TREE_SHA256" ] || die "upstream files do not match the pinned checksum (got $GOT). Refusing to run them."
ok "verified"

# ---------------------------------------------------------------- 5. driver + services (upstream's own installer)
step "Installing the speaker driver (DKMS) and services"
installed_ok() {
    dkms status max98390-hda/1.0 2>/dev/null | grep -F "$(uname -r)" | grep -q installed || return 1
    [ -f /etc/modules-load.d/max98390-hda.conf ] || return 1
    [ -f /etc/systemd/system/max98390-hda-i2c-setup.service ] || return 1
    [ -x /usr/local/sbin/max98390-hda-i2c-setup.sh ] || return 1
    cmp -s "$UP/src/max98390_hda.c" /usr/src/max98390-hda-1.0/src/max98390_hda.c || return 1
    diff -rq "$UP/src" /usr/src/max98390-hda-1.0/src >/dev/null 2>&1
}
if installed_ok; then
    ok "already installed for $(uname -r) (same source) - skipping rebuild"
else
    # Upstream's installer: builds + installs the DKMS module, handles Secure Boot signing and MOK enrollment,
    # installs the helper scripts, the two systemd units and /etc/modules-load.d/max98390-hda.conf.
    sudo bash "$UP/install.sh" </dev/null || die "the driver installer failed (see its output above)."
fi
sudo systemctl enable --now max98390-hda-i2c-setup.service >/dev/null 2>&1 \
    || warn "could not start max98390-hda-i2c-setup.service now; it will run at the next boot"

# ---------------------------------------------------------------- 6. EasyEffects preset + autostart (your user)
step "Setting up EasyEffects"
if PRESET_OUT="$(bash "$UP/easyeffects-presets/install-presets.sh" 2>&1)"; then
    printf '%s\n' "$PRESET_OUT" | sed '/^Next steps/,$d'
else
    printf '%s\n' "$PRESET_OUT" >&2; warn "preset install reported a problem"
fi

want_autostart() { cat <<'EOT'
[Desktop Entry]
Name=Easy Effects (speaker EQ service)
Comment=Background audio effects for the Galaxy Book speakers
Exec=easyeffects --service-mode --hide-window
Icon=com.github.wwmm.easyeffects
Terminal=false
Type=Application
X-GNOME-Autostart-enabled=true
EOT
}
mkdir -p "$(dirname "$AUTOSTART_FILE")"
if [ ! -e "$AUTOSTART_FILE" ]; then
    want_autostart > "$AUTOSTART_FILE"; ok "created $AUTOSTART_FILE"
elif want_autostart | cmp -s - "$AUTOSTART_FILE"; then
    ok "autostart already in place"
else
    warn "$AUTOSTART_FILE already exists with different content - left alone"
fi

if [ "$NO_PRESET_LOAD" = 0 ] && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; } && command -v easyeffects >/dev/null 2>&1; then
    if ! pgrep -x easyeffects >/dev/null 2>&1; then
        (setsid nohup easyeffects --service-mode --hide-window >/dev/null 2>&1 &)
        sleep 4
    fi
    if easyeffects -l "$PRESET_NAME" >/dev/null 2>&1; then ok "loaded preset $PRESET_NAME"
    else warn "could not load $PRESET_NAME automatically; open EasyEffects > Presets > Output and pick it"; fi
fi

# ---------------------------------------------------------------- done
step "Done"
say "Reboot once so the driver and the amplifier service start from a clean boot."
[ "$SECURE" = 1 ] && say "Secure Boot: complete the MOK enrollment screen at that reboot (password you just chose), or the speakers stay silent."
say "Undo everything with: $HERE/uninstall.sh"
