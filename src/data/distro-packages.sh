#!/usr/bin/env bash
# Package lists for gnome-voice-typing. This is the only copy.
# install.sh sources it. bin/system-deps (the in-app Install button) runs it with --json.
#
# Source it, then call:  gvt_distro_packages dnf|apt|pacman|""
# That sets the globals RUNTIME, BUILD, GPUPKG, PQ and YD_SOURCE.

gvt_distro_packages() {
  local pm="${1:-}"
  RUNTIME=""
  BUILD=""
  GPUPKG=""
  PQ="true"
  YD_SOURCE=""
  case "$pm" in
    dnf)
      RUNTIME="python3 python3-gobject at-spi2-core pipewire-utils pulseaudio-utils wl-clipboard ydotool"
      BUILD="git cmake gcc-c++ make ninja-build glslc vulkan-headers vulkan-loader-devel spirv-headers-devel"
      GPUPKG="vulkan-tools mesa-vulkan-drivers"
      PQ="rpm -q --quiet"
      ;;
    apt)
      RUNTIME="python3 python3-venv python3-gi gir1.2-atspi-2.0 pipewire-bin pulseaudio-utils wl-clipboard ydotool"
      BUILD="git cmake g++ make ninja-build libvulkan-dev glslc spirv-headers"
      GPUPKG="vulkan-tools mesa-vulkan-drivers"
      PQ="dpkg -s"
      # Ubuntu 24.04 / Debian 12 ship ydotool 0.1.x; install.sh builds 1.0.4 instead of installing that package.
      YD_SOURCE="git cmake g++ make"
      ;;
    pacman)
      RUNTIME="python python-gobject at-spi2-core pipewire libpulse wl-clipboard ydotool"
      BUILD="git cmake gcc make ninja vulkan-headers vulkan-icd-loader spirv-headers shaderc"
      GPUPKG="vulkan-tools"
      PQ="pacman -Qi"
      ;;
  esac
}

# Exit 0 when apt's ydotool candidate is missing or 0.x (too old: 1.x is required).
gvt_apt_ydotool_is_old() {
  command -v apt-cache >/dev/null 2>&1 || return 1
  local v
  v="$(apt-cache policy ydotool 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  case "$v" in ""|"(none)"|0.*) return 0;; *) return 1;; esac
}

# When this file is sourced, stop here. The JSON printer below is only for `distro-packages.sh --json`.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0 2>/dev/null || exit 0
fi

set -euo pipefail

_json_str() {
  local s="$1"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

_json_arr() {
  local first=1 w
  printf '['
  for w in "$@"; do
    if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
    _json_str "$w"
  done
  printf ']'
}

_emit_json() {
  local first=1 pm
  printf '{'
  for pm in dnf apt pacman; do
    gvt_distro_packages "$pm"
    if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
    printf '"%s":{"runtime":' "$pm"
    # shellcheck disable=SC2086
    _json_arr $RUNTIME
    printf ',"build":'
    # shellcheck disable=SC2086
    _json_arr $BUILD
    printf ',"gpu":'
    # shellcheck disable=SC2086
    _json_arr $GPUPKG
    printf ',"query":'
    _json_str "$PQ"
    printf ',"ydotool_source":'
    # shellcheck disable=SC2086
    _json_arr $YD_SOURCE
    printf '}'
  done
  printf '}\n'
}

case "${1:-}" in
  --json) _emit_json;;
  --apt-ydotool-old) gvt_apt_ydotool_is_old;;
  *) echo "usage: distro-packages.sh --json | --apt-ydotool-old" >&2; exit 2;;
esac
