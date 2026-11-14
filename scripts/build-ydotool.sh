#!/usr/bin/env bash
# Builds ydotool + ydotoold 1.0.4 from source into <install dir>/ext/bin. Only used when your distribution ships an older,
# incompatible ydotool (for example 0.1.8 on Ubuntu 24.04 / Debian 12). ydotool is AGPL-3.0 (https://github.com/ReimuNotMoe/ydotool);
# it is built on your machine and run as a separate program. Needs: git, cmake, a C compiler. No sudo.
set -euo pipefail
DATA="${1:-${XDG_DATA_HOME:-$HOME/.local/share}/gnome-voice-typing}"
REPO=https://github.com/ReimuNotMoe/ydotool
COMMIT=57ba7d0af525e82da2de0e275d169477f293b197        # tag v1.0.4, fetched by commit id
BUILD="${GVT_BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/gnome-voice-typing/build}/ydotool"
for c in git cmake cc; do command -v "$c" >/dev/null || { echo "build-ydotool: missing '$c'" >&2; exit 2; }; done
mkdir -p "$BUILD" "$DATA/ext/bin"; cd "$BUILD"
if [ "$(git rev-parse HEAD 2>/dev/null || true)" != "$COMMIT" ]; then
  [ -d .git ] || git init -q .
  git fetch -q --depth 1 "$REPO" "$COMMIT"; git checkout -q --force FETCH_HEAD
fi
[ "$(git rev-parse HEAD)" = "$COMMIT" ] || { echo "build-ydotool: unexpected source commit" >&2; exit 3; }
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_DOCS=OFF >"$BUILD/cmake.log" 2>&1 || { tail -20 "$BUILD/cmake.log" >&2; exit 4; }
cmake --build build --target ydotool ydotoold -j "$(nproc 2>/dev/null || echo 2)" >"$BUILD/build.log" 2>&1 || { tail -20 "$BUILD/build.log" >&2; exit 5; }
install -m 755 build/ydotool build/ydotoold "$DATA/ext/bin/"
cp -f LICENSE "$DATA/ext/ydotool-LICENSE" 2>/dev/null || true
echo "build-ydotool: OK ($DATA/ext/bin/ydotool, ydotoold)"
