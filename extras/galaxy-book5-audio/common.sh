# shellcheck shell=bash
# shellcheck disable=SC2034  # variables are used by the scripts that source this file
# Shared by install.sh and uninstall.sh (sourced, not run directly).

# ---- the upstream project this extra is built on (NOT copied into this repo) ----
UPSTREAM_REPO='Andycodeman/samsung-galaxy-book-linux-fixes'
UPSTREAM_COMMIT='a6b13c5dd6b5ca5bccca3e8c417c77e3a3a0d138'
UPSTREAM_URL="https://github.com/$UPSTREAM_REPO/archive/$UPSTREAM_COMMIT.tar.gz"
# sha256 over the sorted per-file sha256 list of upstream's speaker-fix/ directory at that commit
# (see tree_hash below); the download is refused if it does not match.
UPSTREAM_TREE_SHA256='cf118abb999f5169642d72947f9426a8ac1999403b3554cd93d58baa69622192'

PRESET_NAME='GalaxyBook-Speakers-Warm'
AUTOSTART_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/easyeffects-service.desktop"

# Hardware detection. These paths can be overridden so the check can be tested against
# saved values (see README, "Testing the hardware check"). Normal use never sets them.
DMI_DIR="${GBA_DMI_DIR:-/sys/class/dmi/id}"
ACPI_DIR="${GBA_ACPI_DIR:-/sys/bus/acpi/devices}"
I2C_DIR="${GBA_I2C_DIR:-/sys/bus/i2c/devices}"
CODEC_GLOB="${GBA_CODEC_GLOB:-/proc/asound/card*/codec#0}"

# sha256 of the five presets in upstream's speaker-fix/easyeffects-presets/ at the pinned commit
# (used by uninstall.sh to remove only presets you have not edited).
PRESET_SHA256S='a4b7caee43bb6b67c8ea70e8988f679b53a75866198a092d06d9e3cab846a904  GalaxyBook-Headphones.json
4cbbfd764fcadb24cd14db51f83755ed45f75aac1e72536a6dd4983ace9fe91e  GalaxyBook-Speakers-Clear.json
ef104c8fb54be50f45f9396d1bb3c4e4e79074f701d8442b53025d193924f90e  GalaxyBook-Speakers-Clear-Loud.json
62c3c5d9eadd39b4ae463b01dd0a29e173185e8249a5bc73ac9e95ee6aea9e10  GalaxyBook-Speakers-Warm.json
7bf96c9de2a4f3eb5329ba555c57f6d69eed7564daa92efc6472bc773a42c6ae  GalaxyBook-Speakers-Warm-Loud.json'

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; Z=$'\e[0m'; else B=; G=; Y=; R=; Z=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$Z"; }
say()  { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$Z" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }

# Print the hardware facts we look at (one per line), then return 0 if this machine is a
# Galaxy Book5 Pro 360 style laptop with MAX98390 speaker amplifiers, else 1.
# Sets HW_REASON to a human-readable explanation.
hw_check() {
    local vendor product sku board codec_sub dmi_ok=0 codec_ok=0 amp_ok=0 f
    vendor="$(cat "$DMI_DIR/sys_vendor" 2>/dev/null || true)"
    product="$(cat "$DMI_DIR/product_name" 2>/dev/null || true)"
    sku="$(cat "$DMI_DIR/product_sku" 2>/dev/null || true)"
    board="$(cat "$DMI_DIR/board_name" 2>/dev/null || true)"

    # 1) DMI: a Samsung 960Q* model (960QHA = Galaxy Book5 Pro 360; board name e.g. NP960QHA-KG1US)
    case "${vendor^^}" in *SAMSUNG*)
        case "$product $sku $board" in *960Q*) dmi_ok=1;; esac;;
    esac

    # 2) Audio codec: Realtek ALC298 with subsystem id 144d:c1da
    for f in $CODEC_GLOB; do
        [ -r "$f" ] || continue
        codec_sub="$(sed -n 's/^Subsystem Id: *//p' "$f" | head -1)"
        if [ "${codec_sub,,}" = "0x144dc1da" ]; then codec_ok=1; break; fi
    done

    # 3) The MAX98390 amplifiers, as the firmware (ACPI) or the I2C bus reports them
    for f in "$ACPI_DIR"/MAX98390:*; do [ -e "$f" ] && amp_ok=1 && break; done
    if [ "$amp_ok" = 0 ]; then
        for f in "$I2C_DIR"/*/name; do
            [ -r "$f" ] || continue
            if grep -qi 'max98390' "$f" 2>/dev/null; then amp_ok=1; break; fi
        done
    fi

    echo "    machine:   ${vendor:-?} / ${product:-?} / ${sku:-?} / ${board:-?}"
    echo "    model match (Samsung 960Q*): $([ $dmi_ok = 1 ] && echo yes || echo no)"
    echo "    codec ALC298 144d:c1da:      $([ $codec_ok = 1 ] && echo yes || echo no)"
    echo "    MAX98390 amplifiers:         $([ $amp_ok = 1 ] && echo yes || echo no)"

    if [ "$amp_ok" = 0 ]; then
        HW_REASON="no MAX98390 speaker amplifiers were found (this fix is only for laptops that have them)"
        return 1
    fi
    if [ "$dmi_ok" = 0 ] && [ "$codec_ok" = 0 ]; then
        HW_REASON="this is not a Samsung Galaxy Book5 Pro 360 (model 960Q*) and the audio codec is not the 144d:c1da one"
        return 1
    fi
    HW_REASON="Samsung Galaxy Book5 Pro 360 class hardware detected"
    return 0
}

# Deterministic hash of a directory tree: sha256 of the sorted "sha256  ./path" list.
tree_hash() {
    ( cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 )
}
