#!/bin/sh
# chime.sh <start|stop|wake> - play a voice-typing chime, non-blocking.
# Does nothing (exit 0) if config.json has "sounds": false. Missing key => enabled.
# Env override: GVT_CONFIG=/path/to/config.json
case "$1" in start|stop|wake) ;; *) echo "usage: ${0##*/} <start|stop|wake>" >&2; exit 2;; esac
D=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || exit 1
CFG=${GVT_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/gnome-voice-typing/config.json}
WAV=$D/sounds/$1.wav
[ -r "$WAV" ] || exit 0
if [ -r "$CFG" ]; then
  if command -v jq >/dev/null 2>&1; then
    [ "$(jq -r 'if (type=="object" and .sounds==false) then "off" else "on" end' "$CFG" 2>/dev/null)" = off ] && exit 0
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("sounds",True) is False else 1)' "$CFG" 2>/dev/null && exit 0
  fi
fi
if command -v pw-play >/dev/null 2>&1; then P=pw-play
elif command -v paplay >/dev/null 2>&1; then P=paplay
else exit 0; fi
setsid "$P" "$WAV" >/dev/null 2>&1 </dev/null &
exit 0
