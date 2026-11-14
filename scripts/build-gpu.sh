#!/usr/bin/env bash
# Builds the Vulkan speech backend (whisper.cpp + parakeet.cpp, plus a tiny C shim) into <install dir>/gpu.
# Called by install.sh; can also be run by hand:  scripts/build-gpu.sh [INSTALL_DIR]
# Needs: git, cmake, a C++ compiler, make or ninja, glslc, Vulkan headers + loader dev files. Runs as your user; no sudo.
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
DATA="${1:-${XDG_DATA_HOME:-$HOME/.local/share}/gnome-voice-typing}"
# whisper.cpp (MIT) pinned to an exact commit: fetched by commit id, so git itself verifies the content.
WHISPER_REPO=https://github.com/ggml-org/whisper.cpp
WHISPER_COMMIT=5922450fbce1ed9d1648babd0d5ac4520055b436
BUILD="${GVT_BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/gnome-voice-typing/build}"
JOBS="${GVT_JOBS:-$(nproc 2>/dev/null || echo 4)}"
for c in git cmake c++ glslc; do command -v "$c" >/dev/null || { echo "build-gpu: missing '$c'" >&2; exit 2; }; done
GPUSRC="$SRC/src/gpu"; [ -f "$GPUSRC/shim.cpp" ] || GPUSRC="$SRC/gpu"        # source tree vs. installed copy
mkdir -p "$BUILD/whisper.cpp" "$DATA/gpu/lib"
cd "$BUILD/whisper.cpp"
if [ "$(git rev-parse HEAD 2>/dev/null || true)" != "$WHISPER_COMMIT" ]; then
  [ -d .git ] || git init -q .
  git fetch -q --depth 1 "$WHISPER_REPO" "$WHISPER_COMMIT"
  git checkout -q --force FETCH_HEAD
fi
[ "$(git rev-parse HEAD)" = "$WHISPER_COMMIT" ] || { echo "build-gpu: unexpected source commit" >&2; exit 3; }
GEN=(); command -v ninja >/dev/null && GEN=(-G Ninja)
cmake -S . -B build "${GEN[@]}" -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DGGML_NATIVE=ON -DBUILD_SHARED_LIBS=ON \
  -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON '-DCMAKE_INSTALL_RPATH=$ORIGIN' >"$BUILD/cmake.log" 2>&1 || { tail -30 "$BUILD/cmake.log" >&2; exit 4; }
cmake --build build -j "$JOBS" --config Release >"$BUILD/build.log" 2>&1 || { tail -40 "$BUILD/build.log" >&2; exit 5; }
rm -f "$DATA"/gpu/lib/lib*.so*
find build -name 'lib*.so*' \( -name 'libwhisper*' -o -name 'libparakeet*' -o -name 'libggml*' \) -exec cp -a {} "$DATA/gpu/lib/" \;
c++ -O2 -shared -fPIC -std=c++17 -I include -I ggml/include "$GPUSRC/shim.cpp" -o "$DATA/gpu/libasrshim.so" \
  -L "$DATA/gpu/lib" -lwhisper -lparakeet -lggml -lggml-base '-Wl,-rpath,$ORIGIN/lib' >"$BUILD/shim.log" 2>&1 || { cat "$BUILD/shim.log" >&2; exit 6; }
[ "$GPUSRC/backend.py" -ef "$DATA/gpu/backend.py" ] || cp "$GPUSRC/backend.py" "$DATA/gpu/backend.py"
cp LICENSE "$DATA/gpu/LICENSE.whisper.cpp" 2>/dev/null || true
echo "build-gpu: OK ($(ls "$DATA/gpu/lib" | wc -l) libraries in $DATA/gpu/lib)"
