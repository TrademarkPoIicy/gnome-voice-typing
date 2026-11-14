#!/usr/bin/env bash
# Builds dist/gnome-voice-typing-<version>.tar.gz (+ .sha256) from this directory. Only tracked source goes in: no models, venv or caches.
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
V="$(cat "$SRC/VERSION")"; NAME="gnome-voice-typing-$V"
OUT="${1:-$SRC/../dist}"; mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
find "$SRC" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
# reproducible: sorted names, fixed owner and mtime
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-11-14 00:00:00 UTC' \
    --exclude=__pycache__ --exclude='*.pyc' --exclude='*.part' --exclude='*.bak*' \
    --transform "s,^\./,$NAME/," -C "$SRC" -cf - . | gzip -n -9 > "$OUT/$NAME.tar.gz"
( cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
echo "$OUT/$NAME.tar.gz"; cat "$OUT/$NAME.tar.gz.sha256"
