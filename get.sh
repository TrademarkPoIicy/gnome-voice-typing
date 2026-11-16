#!/usr/bin/env bash
# One-line installer for gnome-voice-typing:
#     curl -fsSL https://raw.githubusercontent.com/TrademarkPoIicy/gnome-voice-typing/main/get.sh | bash
# Extra install.sh options go after "bash -s --", e.g.  ... | bash -s -- --cpu-only
# Downloads the current source from GitHub into a temporary folder, runs ./install.sh there, then removes the folder.
# Re-running it upgrades an existing install and keeps your models and settings.
set -euo pipefail
REPO="${GVT_REPO:-TrademarkPoIicy/gnome-voice-typing}"; REF="${GVT_REF:-main}"
for t in curl tar; do command -v "$t" >/dev/null || { echo "gnome-voice-typing: '$t' is required" >&2; exit 1; }; done
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "Downloading gnome-voice-typing ($REF) from github.com/$REPO ..."
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar xz -C "$TMP" --strip-components=1
cd "$TMP"
echo "Version $(cat VERSION)"
# piped through curl | bash, stdin is the script: give the installer's questions the terminal instead
if [ -t 0 ] || [ ! -r /dev/tty ]; then ./install.sh "$@"; else ./install.sh "$@" < /dev/tty; fi
