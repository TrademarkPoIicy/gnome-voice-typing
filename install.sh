#!/usr/bin/env bash
# gnome-voice-typing installer.  One command, safe to re-run.
#   ./install.sh            interactive install into your home directory (asks before anything that needs sudo)
#   ./install.sh --dry-run  show exactly what would happen; changes nothing
# Run  ./install.sh --help  for all options.
set -uo pipefail

APP=gnome-voice-typing
UUID='voice-typing@gnome-voice-typing.github.io'
SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VERSION="$(cat "$SRC/VERSION" 2>/dev/null || echo dev)"
SUPPORTED_MIN=45; SUPPORTED_MAX=51          # GNOME Shell major versions listed in the extension's metadata.json (tested: 51)

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP"
EXTDIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop"
LOCALBIN="$HOME/.local/bin"
STATE="$DATA/install-state.json"

DRY=0; YES=0; WANT_GPU=auto; NO_SYSDEPS=0; NO_UDEV=0; NO_GNOME=0; NO_AUTOSTART=0; NO_START=0; NO_MODELS=0; FORCE=0
BINDING='<Super>h'; MODEL=''; CPU_FALLBACK=ask; NO_KEYBIND=0

usage() { cat <<EOT
gnome-voice-typing $VERSION installer

usage: ./install.sh [options]
  --dry-run          print what would be done (nothing is changed, nothing is downloaded)
  -y, --yes          answer "yes" to every question, INCLUDING the sudo steps (package install, udev rule)
  --gpu | --cpu-only use / skip the graphics-card (Vulkan) engine (default: detect and ask)
  --model ID         first model to download (default: parakeet-gpu with a GPU, parakeet on CPU); see: voice-typing-models list
  --no-cpu-fallback  with the GPU engine, do not also download the small CPU fallback model
  --no-models        do not download any model now
  --binding ACCEL    keyboard shortcut (default '<Super>h'; e.g. '<Super><Alt>h')
  --no-keybinding    do not create a keyboard shortcut
  --no-system-deps   do not install distro packages (you must have them already)
  --no-udev          do not install the /dev/uinput udev rule (typing then needs GNOME's remote-control portal)
  --no-autostart     do not start the background service at login
  --no-start         do not start the background service now
  --skip-gnome       do not touch GNOME settings or install the extension (containers / headless testing)
  --force            continue on a GNOME Shell version outside $SUPPORTED_MIN-$SUPPORTED_MAX
  -h, --help         this text

Files go to: $DATA (program, models), $CONF (your settings), $STATE_DIR (log), $EXTDIR (extension).
Undo everything with:  $DATA/uninstall.sh
EOT
}
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; -y|--yes) YES=1;; --gpu) WANT_GPU=yes;; --cpu-only) WANT_GPU=no;;
  --model) MODEL="${2:-}"; shift;; --no-cpu-fallback) CPU_FALLBACK=no;; --no-models) NO_MODELS=1;;
  --binding) BINDING="${2:-}"; shift;; --no-keybinding) NO_KEYBIND=1;;
  --no-system-deps) NO_SYSDEPS=1;; --no-udev) NO_UDEV=1;; --no-autostart) NO_AUTOSTART=1;; --no-start) NO_START=1;;
  --skip-gnome) NO_GNOME=1;; --force) FORCE=1;; -h|--help) usage; exit 0;;
  *) echo "unknown option: $1" >&2; usage >&2; exit 2;; esac; shift; done

# ---------------------------------------------------------------- output helpers
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; Z=$'\e[0m'; else B=; G=; Y=; R=; D=; Z=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$Z"; }
say()  { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$Z" "$*"; }
die()  { printf '\n%sERROR:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }
dry()  { printf '    %s[dry-run]%s %s\n' "$D" "$Z" "$*"; }
# run CMD...: execute, or just print in --dry-run
run()  { if [ "$DRY" = 1 ]; then dry "$*"; return 0; fi; "$@"; }
ask()  { # ask "question" y|n  -> 0 for yes
  local q="$1" def="${2:-y}" a hint='[Y/n]'; [ "$def" = n ] && hint='[y/N]'
  if [ "$YES" = 1 ]; then printf '    ? %s %s yes (--yes)\n' "$q" "$hint"; return 0; fi
  if [ "$DRY" = 1 ] || [ ! -t 0 ]; then printf '    ? %s %s %s (non-interactive)\n' "$q" "$hint" "$def"; [ "$def" = y ]; return; fi
  read -r -p "    ? $q $hint " a; a="${a:-$def}"; case "$a" in [Yy]*) return 0;; *) return 1;; esac
}
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=sudo; [ "$(id -u)" = 0 ] && SUDO=

[ "$(id -u)" = 0 ] && die "Run this as your normal desktop user, not root. It asks for sudo only where needed."
[ "$DRY" = 1 ] && printf '%s*** DRY RUN: nothing will be changed or downloaded ***%s\n' "$Y" "$Z"
printf '%sgnome-voice-typing %s%s - offline voice typing for GNOME\n' "$B" "$VERSION" "$Z"
printf 'Everything runs on this computer. The only network use is this installer (distro packages, Python packages, whisper.cpp source, speech models).\n'

# ---------------------------------------------------------------- 1. system checks
step "1/10 Checking your system"
[ "$(uname -s)" = Linux ] || die "Linux only."
case "$(uname -m)" in x86_64|aarch64) ok "CPU architecture: $(uname -m)";; *) warn "untested CPU architecture: $(uname -m)";; esac
eval "$(. /etc/os-release 2>/dev/null; printf 'DISTRO=%q LIKE=%q PRETTY=%q' "${ID:-unknown}" " ${ID_LIKE:-} " "${PRETTY_NAME:-}")"        # (in a subshell: os-release also defines VERSION)
PM=''
case "$DISTRO$LIKE" in *fedora*|*rhel*|*centos*) PM=dnf;; *debian*|*ubuntu*) PM=apt;; *arch*|*manjaro*) PM=pacman;; esac
if [ -n "$PM" ]; then ok "Distribution: ${PRETTY:-$DISTRO} (package manager: $PM)"
else warn "Distribution '${PRETTY:-$DISTRO}' is not one of Fedora / Debian / Ubuntu / Arch: I cannot install packages for you (see README for the list)."; NO_SYSDEPS=1; fi
case "${XDG_SESSION_TYPE:-}" in wayland) ok "Session: Wayland";; x11) warn "You are on an X11 session. It should work, but it is only tested on Wayland.";; *) warn "No desktop session detected (XDG_SESSION_TYPE is empty).";; esac
if [ "$NO_GNOME" = 1 ]; then warn "--skip-gnome: GNOME checks, settings and the extension are skipped."; SHELLV=0
else
  have gnome-shell || die "GNOME Shell was not found. This tool is a GNOME Shell extension plus a background service; it needs GNOME $SUPPORTED_MIN-$SUPPORTED_MAX."
  SHELLV="$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -1)"; SHELLV="${SHELLV:-0}"
  if [ "$SHELLV" -lt "$SUPPORTED_MIN" ] || [ "$SHELLV" -gt "$SUPPORTED_MAX" ]; then
    if [ "$FORCE" = 1 ]; then warn "GNOME Shell $SHELLV is outside the supported range $SUPPORTED_MIN-$SUPPORTED_MAX; continuing because of --force."
    else die "GNOME Shell $SHELLV is not supported (supported: $SUPPORTED_MIN-$SUPPORTED_MAX). Nothing was changed. Use --force to try anyway."; fi
  else ok "GNOME Shell $SHELLV (supported: $SUPPORTED_MIN-$SUPPORTED_MAX; tested on 51)"; fi
fi

# GPU: a real Vulkan device (not the llvmpipe software renderer)
GPU_NAME=''
if have vulkaninfo; then
  # one block per "GPUn:" entry; pick the first real GPU (discrete, integrated or virtual), never the llvmpipe/lavapipe software renderers
  GPU_NAME="$(vulkaninfo --summary 2>/dev/null | awk '
    function flush() { if (!done && real && name != "" && name !~ /llvmpipe|lavapipe|SwiftShader/) { print name; done = 1 } real = 0; name = "" }
    /^GPU[0-9]+:/ { flush(); next }
    /deviceType/ { if ($0 ~ /(DISCRETE|INTEGRATED|VIRTUAL)_GPU/) real = 1 }
    /deviceName/ { n = $0; sub(/^[^=]*= */, "", n); name = n }
    END { flush() }')"
fi
if [ -n "$GPU_NAME" ]; then ok "Vulkan graphics card found: $GPU_NAME"
elif have vulkaninfo; then say "No Vulkan graphics card found (only software rendering): the CPU engine will be used."
else say "vulkaninfo is not installed, so the graphics card could not be checked (it will be installed with the packages below if you use the GPU engine)."; fi

USE_GPU=0
case "$WANT_GPU" in
  no) say "CPU engine (--cpu-only).";;
  yes) USE_GPU=1; ok "GPU engine requested (--gpu).";;
  auto) if [ -n "$GPU_NAME" ]; then
      echo "    The GPU engine (whisper.cpp / parakeet.cpp on Vulkan) is much faster and uses almost no CPU, but it is compiled on your machine"
      echo "    (needs git, cmake, a C++ compiler and glslc; takes roughly 5-15 minutes)."
      ask "Use the graphics card ($GPU_NAME)?" y && USE_GPU=1
    fi;;
esac
[ "$USE_GPU" = 1 ] && [ "$(uname -m)" != x86_64 ] && warn "GPU engine is only tested on x86_64."

# ---------------------------------------------------------------- 2. packages
step "2/10 System packages"
# Package names live in src/data/distro-packages.sh. The in-app Install button reads that same file,
# so the prompt and this installer cannot drift apart.
. "$SRC/src/data/distro-packages.sh"
gvt_distro_packages "$PM"
# ydotool 1.x is required (--socket-path). Some distros still ship 0.1.x (Ubuntu 24.04, Debian 12): then it is built from source instead.
YD_BUILD=0
if [ "$PM" = apt ] && gvt_apt_ydotool_is_old; then
  YD_BUILD=1
  YDV="$(apt-cache policy ydotool 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  RUNTIME="${RUNTIME/ ydotool/}"
  say "This distribution packages ydotool ${YDV:-(none)} (too old: 1.x is needed); ydotool 1.0.4 will be built from source (AGPL-3.0)."
fi
WANT="$RUNTIME"; [ "$USE_GPU" = 1 ] && WANT="$WANT $BUILD $GPUPKG"
if [ "$YD_BUILD" = 1 ] && [ "$USE_GPU" != 1 ]; then WANT="$WANT $YD_SOURCE"; fi
MISSING=''
for p in $WANT; do $PQ "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
MISSING="${MISSING# }"
if [ "$NO_SYSDEPS" = 1 ]; then
  say "Skipping package installation (--no-system-deps / unsupported distro). Checking the tools directly:"
  for c in python3 pw-record pactl wl-copy ydotool ydotoold; do have "$c" && ok "$c" || warn "missing: $c"; done
  if [ "$USE_GPU" = 1 ]; then for c in git cmake c++ glslc; do have "$c" && ok "$c" || warn "missing (needed to build the GPU engine): $c"; done; fi
elif [ -z "$MISSING" ]; then ok "All required packages are already installed."
else
  echo "    Missing packages: $MISSING"
  echo "    These are installed with your distro's package manager. That needs administrator rights, so I will run:"
  case "$PM" in dnf) CMD="$SUDO dnf install -y $MISSING";; apt) CMD="$SUDO apt-get install -y $MISSING";; pacman) CMD="$SUDO pacman -S --needed --noconfirm $MISSING";; esac
  echo "      $CMD"
  if ask "Install these packages now?" y; then
    if [ "$DRY" = 1 ]; then dry "$CMD"; else
      [ "$PM" = apt ] && { $SUDO apt-get update || warn "apt-get update failed (continuing)"; }
      $CMD || die "Package installation failed. Fix the problem (or install the packages yourself) and run ./install.sh again."
    fi
  else warn "Skipped. The install may fail below if tools are missing."; fi
fi
YD_SYS_OK=0
if have ydotoold && ydotoold --help 2>&1 | grep -q -- '--socket-path'; then YD_SYS_OK=1; fi
[ "$DRY" = 0 ] && { have python3 || die "python3 is required."; }

# ---------------------------------------------------------------- 3. copy files
step "3/10 Installing the program into $DATA"
mkdir_p() { run mkdir -p "$@"; }
if [ -e "$DATA" ]; then say "Existing install found: upgrading the program files, keeping your models, settings and Python environment."; fi
mkdir_p "$DATA" "$CONF" "$STATE_DIR" "$LOCALBIN"
if [ "$DRY" = 1 ]; then dry "copy src/{bin,gpu,sounds,data,lib} -> $DATA, uninstall.sh -> $DATA/uninstall.sh, license files -> $DATA/"; else
  for d in bin gpu sounds data lib; do mkdir -p "$DATA/$d"; cp -a "$SRC/src/$d/." "$DATA/$d/"; done
  [ -e "$DATA/gpu/lib" ] || mkdir -p "$DATA/gpu/lib"
  cp -a "$SRC/uninstall.sh" "$DATA/uninstall.sh"; mkdir -p "$DATA/scripts"; cp -a "$SRC/scripts/." "$DATA/scripts/"; cp -a "$SRC/VERSION" "$SRC/LICENSE" "$SRC/THIRD_PARTY_LICENSES.md" "$DATA/"
  chmod +x "$DATA"/bin/* "$DATA/uninstall.sh" "$DATA"/scripts/*.sh; mkdir -p "$DATA/models"
  ln -sfn "$DATA/bin/voice-typing" "$LOCALBIN/voice-typing"; ln -sfn "$DATA/bin/voice-typing-models" "$LOCALBIN/voice-typing-models"
  [ -f "$STATE" ] || echo '{}' > "$STATE"
fi
[ "$DRY" = 1 ] && dry "link $LOCALBIN/voice-typing and voice-typing-models -> $DATA/bin"
APPDIR="$DATA"; [ "$DRY" = 1 ] && APPDIR="$SRC/src"      # in --dry-run nothing is copied: read from the unpacked source instead
ok "Program files in place."
# helper to record things in the install-state file (read by uninstall.sh)
state_set() { # state_set KEY JSON
  [ "$DRY" = 1 ] && return 0
  python3 - "$STATE" "$1" "$2" <<'PYE'
import json, sys
p, k, v = sys.argv[1:4]
try: s = json.load(open(p))
except Exception: s = {}
s[k] = json.loads(v); json.dump(s, open(p, 'w'), indent=2)
PYE
}
state_set version "\"$VERSION\""; state_set paths "{\"data\": \"$DATA\", \"config\": \"$CONF\", \"state\": \"$STATE_DIR\", \"extension\": \"$EXTDIR\", \"autostart\": \"$AUTOSTART\", \"bin\": \"$LOCALBIN\"}"

# ydotool 1.x is needed for typing; build it only if the system one is missing/too old
if [ "$DRY" = 1 ]; then [ "$YD_SYS_OK" = 1 ] || [ "$YD_BUILD" = 0 ] || dry "scripts/build-ydotool.sh  (ydotool 1.0.4 from source into $DATA/ext/bin)"
else
  [ "$YD_SYS_OK" = 0 ] && [ -x "$DATA/ext/bin/ydotoold" ] && YD_SYS_OK=1
  if [ "$YD_SYS_OK" = 0 ] && have git && have cmake && have cc; then
    say "Building ydotool 1.0.4 from source (the installed one is missing or too old) ..."
    if "$DATA/scripts/build-ydotool.sh" "$DATA"; then YD_SYS_OK=1; state_set ydotool_built true; else warn "Building ydotool failed."; fi
  fi
  [ "$YD_SYS_OK" = 1 ] || warn "A compatible ydotool/ydotoold was not found: typing will fall back to GNOME's remote-control portal (shows a permission prompt and an indicator)."
fi

# ---------------------------------------------------------------- 4. python
step "4/10 Python environment"
PYCAND=''
for c in python3.12 python3.13 python3.11 python3.10 python3.14 python3; do
  have "$c" || continue
  "$c" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null && { PYCAND="$PYCAND $c"; }
done
if [ -x "$DATA/venv/bin/python" ] && "$DATA/venv/bin/python" -c 'import numpy, sherpa_onnx' 2>/dev/null; then ok "Python environment already set up ($("$DATA/venv/bin/python" -V))."
elif [ "$DRY" = 1 ]; then
  dry "python -m venv $DATA/venv   (candidates:${PYCAND:- none})"; dry "pip install -r requirements.txt   (numpy, sherpa-onnx)"
  dry "if no wheels exist for your Python: install 'uv' from PyPI in a temporary venv and let it create a Python 3.12 environment"
else
  VENV_OK=0
  for c in $PYCAND; do
    say "Trying $($c -V 2>&1) ..."
    rm -rf "$DATA/venv"
    if "$c" -m venv "$DATA/venv" && "$DATA/venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$DATA/data/requirements.txt" && "$DATA/venv/bin/python" -c 'import numpy, sherpa_onnx'; then VENV_OK=1; ok "Python environment ready ($("$DATA/venv/bin/python" -V))."; break
    else warn "$c could not install the Python packages (no wheels for this Python version?)."; fi
  done
  if [ "$VENV_OK" = 0 ]; then
    say "Falling back to a managed Python 3.12 (via 'uv' from PyPI; it downloads a standalone Python from GitHub)."
    TMPV="$(mktemp -d)"; rm -rf "$DATA/venv"
    if python3 -m venv "$TMPV/u" && "$TMPV/u/bin/pip" install --quiet --disable-pip-version-check uv && "$TMPV/u/bin/uv" venv --quiet --python 3.12 "$DATA/venv" && "$TMPV/u/bin/uv" pip install --quiet --python "$DATA/venv/bin/python" -r "$DATA/data/requirements.txt" && "$DATA/venv/bin/python" -c 'import numpy, sherpa_onnx'; then
      VENV_OK=1; ok "Python environment ready ($("$DATA/venv/bin/python" -V))."
    fi; rm -rf "$TMPV"
  fi
  [ "$VENV_OK" = 1 ] || die "Could not create the Python environment. See the messages above."
fi

# ---------------------------------------------------------------- 5. GPU engine
step "5/10 Speech engine"
if [ "$USE_GPU" = 1 ]; then
  if [ "$DRY" = 1 ]; then dry "scripts/build-gpu.sh  (pinned whisper.cpp commit, Vulkan, built into $DATA/gpu; log in ~/.cache/$APP/build)"
  elif [ -f "$DATA/gpu/libasrshim.so" ] && "$DATA/venv/bin/python" "$DATA/gpu/backend.py" --probe >/dev/null 2>&1; then ok "GPU engine already built and working."
  else
    say "Building the GPU engine (this takes a while; build log: ~/.cache/$APP/build/build.log) ..."
    if "$DATA/scripts/build-gpu.sh" "$DATA"; then
      if G="$("$DATA/venv/bin/python" "$DATA/gpu/backend.py" --probe 2>/dev/null)"; then ok "GPU engine works: $G"
      else warn "The GPU engine was built but no usable Vulkan graphics card was found: using the CPU engine."; USE_GPU=0; fi
    else warn "Building the GPU engine failed (see ~/.cache/$APP/build/*.log). Using the CPU engine instead; re-run ./install.sh --gpu to retry."; USE_GPU=0; fi
  fi
else say "Using the CPU engine (sherpa-onnx). Nothing to build."; fi

# ---------------------------------------------------------------- 6. models
step "6/10 Speech models"
if [ "$USE_GPU" = 1 ]; then DEFMODEL=parakeet-gpu; else DEFMODEL=parakeet; fi
[ -n "$MODEL" ] || MODEL="$DEFMODEL"
python3 -c "import json,sys; sys.exit(0 if sys.argv[1] in json.load(open(sys.argv[2]))['models'] else 1)" "$MODEL" "$APPDIR/data/models-registry.json" || die "Unknown model '$MODEL'. Known ids: $(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['models']))" "$APPDIR/data/models-registry.json")"
MODELS_TO_GET="$MODEL"
if [ "$USE_GPU" = 1 ] && [ "$MODEL" = "$DEFMODEL" ] && [ "$CPU_FALLBACK" != no ]; then
  echo "    A small CPU model is also used automatically if the graphics card is ever unavailable (e.g. a driver problem)."
  ask "Also download the CPU fallback model (Parakeet int8, ~460 MB)?" y && MODELS_TO_GET="$MODELS_TO_GET parakeet"
fi
if [ "$NO_MODELS" = 1 ]; then warn "--no-models: no model downloaded. Install one later with: voice-typing-models install $MODEL"
else
  echo "    Models are downloaded only from their official hosts (Hugging Face, GitHub) and checked against pinned SHA-256 checksums."
  echo "    Licenses: see THIRD_PARTY_LICENSES.md (Parakeet is CC-BY-4.0: credit NVIDIA if you redistribute or build on it; Whisper and Moonshine are MIT)."
  for m in $MODELS_TO_GET; do
    if [ "$DRY" = 1 ]; then python3 "$APPDIR/bin/models_tool.py" install --dry-run "$m" | sed 's/^/    /'
    else "$DATA/bin/voice-typing-models" install "$m" || { [ "$m" = "$MODEL" ] && die "Model download failed. Run ./install.sh again to resume."; warn "Optional model $m failed; continuing."; }; fi
  done
  [ "$DRY" = 1 ] || state_set models "$(python3 -c "import json,sys; print(json.dumps(sys.argv[1].split()))" "$MODELS_TO_GET")"
fi
say "Only one model was downloaded now. Every other model is one click away: click the gear on the voice bar and press Download next to it (Uninstall removes it again)."
say "From a terminal: voice-typing-models list | install ID | remove ID"
# config: create only if it does not exist (never overwrite your settings)
if [ -f "$CONF/config.json" ]; then ok "Keeping your existing settings ($CONF/config.json)."
elif [ "$DRY" = 1 ]; then dry "write $CONF/config.json (model $MODEL)"
else
  python3 - "$DATA/data/config.default.json" "$CONF/config.json" "$MODEL" "$DATA/data/models.json" <<'PYE'
import json, sys
c = json.load(open(sys.argv[1])); m = sys.argv[3]
e = next((x for x in json.load(open(sys.argv[4])) if x['id'] == m), None)
if e: c['backend'] = e['backend']; c['model'] = m; c['device'] = 'gpu' if e['backend'] == 'vk' else 'cpu'
json.dump(c, open(sys.argv[2], 'w'), indent=2)
PYE
  ok "Settings written to $CONF/config.json"
fi

# ---------------------------------------------------------------- 7. uinput
step "7/10 Permission to type into other windows (/dev/uinput)"
RULE=/etc/udev/rules.d/70-$APP-uinput.rules; MODLOAD=/etc/modules-load.d/$APP-uinput.conf
if [ -w /dev/uinput ] 2>/dev/null; then ok "/dev/uinput is already writable for you; nothing to do."
elif [ "$NO_UDEV" = 1 ]; then warn "--no-udev: skipped. Typing will use GNOME's remote-control portal instead (a one-time permission prompt and a screen-sharing-style indicator)."
else
  cat <<EOT
    To type the recognised words into the window you are using, the program creates a virtual keyboard through /dev/uinput (via ydotool).
    By default only root may do that. This step (needs sudo, done once) installs two small files:
      $RULE     -> lets the logged-in desktop user use /dev/uinput (udev "uaccess" tag)
      $MODLOAD   -> loads the uinput kernel module at boot
    Nothing else changes; uninstall.sh removes both again. Without it, typing falls back to GNOME's remote-control portal.
EOT
  if ask "Install the udev rule now (uses sudo)?" y; then
    if [ "$DRY" = 1 ]; then dry "$SUDO install -m 644 70-$APP-uinput.rules $RULE; echo uinput | $SUDO tee $MODLOAD; $SUDO modprobe uinput; $SUDO udevadm control --reload; $SUDO udevadm trigger --name-match=uinput --action=add"
    else
      if $SUDO install -m 644 "$SRC/src/system/70-$APP-uinput.rules" "$RULE" && echo uinput | $SUDO tee "$MODLOAD" >/dev/null && $SUDO modprobe uinput && $SUDO udevadm control --reload && $SUDO udevadm trigger --name-match=uinput --action=add; then
        state_set udev '{"rule": "'"$RULE"'", "modules_load": "'"$MODLOAD"'"}'; sleep 1
        [ -w /dev/uinput ] && ok "/dev/uinput is now usable by you." || warn "Rule installed, but /dev/uinput is not writable yet; you may need to log out and in again."
      else warn "Could not install the udev rule. Typing will use the remote-control portal."; fi
    fi
  else warn "Skipped. Typing will use GNOME's remote-control portal."; fi
fi

# ---------------------------------------------------------------- 8. extension + shortcut
step "8/10 GNOME extension, keyboard shortcut, accessibility"
if [ "$NO_GNOME" = 1 ]; then warn "--skip-gnome: skipped."
else
  # extension: a copy (not a symlink), metadata written for the detected GNOME versions
  if [ "$DRY" = 1 ]; then dry "copy src/extension/* -> $EXTDIR (shell-version: $SUPPORTED_MIN..$SUPPORTED_MAX)"; else
    mkdir -p "$EXTDIR"; cp -a "$SRC/src/extension/." "$EXTDIR/"
    python3 - "$EXTDIR/metadata.json" "$SUPPORTED_MIN" "$SUPPORTED_MAX" "$SHELLV" <<'PYE'
import json, sys
p, lo, hi, cur = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
m = json.load(open(p)); v = list(range(lo, hi + 1))
if cur and cur not in v: v.append(cur)       # --force on an unlisted version: let the shell load it
m['shell-version'] = [str(x) for x in sorted(v)]; json.dump(m, open(p, 'w'), indent=2)
PYE
    state_set extension_installed true
  fi
  ok "Extension files: $EXTDIR"
  CONFLICT_ARG=''
  if [ "$NO_KEYBIND" = 1 ]; then warn "--no-keybinding: no shortcut will be created. Bind your own to: $DATA/bin/voice-typing"
  else
    while :; do
      CJ="$(python3 "$APPDIR/lib/gvt_gnome.py" conflicts "$BINDING" 2>/dev/null || echo '[]')"
      NC="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$CJ")"
      if [ "$NC" = 0 ]; then break; fi
      python3 -c "
import json,sys
for c in json.loads(sys.argv[1]): print('    ! %s is already used by: %s' % (c['accel'], c['desc']))" "$CJ"
      echo "    To use $BINDING, that shortcut has to be turned off (the old value is saved and uninstall.sh puts it back)."
      if ask "Turn the conflicting shortcut off and use $BINDING?" y; then CONFLICT_ARG=--resolve-conflicts; break; fi
      if [ "$YES" = 1 ] || [ "$DRY" = 1 ] || [ ! -t 0 ]; then BINDING='<Super><Alt>h'; say "Using $BINDING instead."; [ "$DRY" = 1 ] && break; continue; fi
      read -r -p "    Enter another shortcut (e.g. <Super><Alt>h): " BINDING; [ -n "$BINDING" ] || BINDING='<Super><Alt>h'
    done
    ok "Shortcut: $BINDING"
  fi
  ARGS=(apply --state "$STATE" --command "$DATA/bin/voice-typing" --uuid "$UUID" $CONFLICT_ARG)
  if [ "$NO_KEYBIND" = 1 ]; then ARGS+=(--binding ''); else ARGS+=(--binding "$BINDING"); fi
  [ "$DRY" = 1 ] && ARGS+=(--dry-run)
  python3 "$APPDIR/lib/gvt_gnome.py" "${ARGS[@]}" | sed 's/^/    /'
  [ "${PIPESTATUS[0]}" = 0 ] || die "Could not update the GNOME settings."
fi

# ---------------------------------------------------------------- 9. autostart
step "9/10 Start at login"
if [ "$NO_AUTOSTART" = 1 ]; then warn "--no-autostart: skipped. Start it yourself with: $DATA/bin/voice-typingd"
elif [ "$DRY" = 1 ]; then dry "write $AUTOSTART (runs $DATA/bin/voice-typingd at login; invisible until you press the shortcut)"
else
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<EOT
[Desktop Entry]
Type=Application
Name=Voice Typing (background service)
Comment=Keeps the speech model loaded so dictation starts instantly. Shows nothing until you press the shortcut.
Exec=$DATA/bin/voice-typingd
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOT
  state_set autostart "\"$AUTOSTART\""; ok "Autostart entry: $AUTOSTART"
fi
if [ "$NO_START" = 0 ] && [ "$DRY" = 0 ]; then
  if "$DATA/bin/voice-typing" status 2>/dev/null | grep -q '"state": "stopped"'; then
    ( setsid "$DATA/bin/voice-typingd" >/dev/null 2>&1 < /dev/null & ); sleep 1; ok "Background service started."
  else ok "Background service is already running (restart it to load the new version: voice-typing quit)."; fi
elif [ "$DRY" = 1 ]; then dry "start $DATA/bin/voice-typingd in the background"; fi

# ---------------------------------------------------------------- 10. done
step "10/10 Done"
if [ "$DRY" = 1 ]; then
  echo "    Dry run finished: nothing was changed. Run ./install.sh without --dry-run to install."
else
  cat <<EOT
    ${G}Installed gnome-voice-typing $VERSION.${Z}

    ${B}Next step: log out and log back in${Z} (needed once, on Wayland, so GNOME Shell loads the new extension).
    After that: press ${B}${BINDING:-your shortcut}${Z} to start dictating, press it again to pause, say "close dictation" or click X to stop.
    Settings (model, graphics card / processor, microphone, pause time): click the gear on the bar.
    Command line: voice-typing status | voice-typing --models        Log: $STATE_DIR/daemon.log
    Uninstall:    $DATA/uninstall.sh
EOT
fi
