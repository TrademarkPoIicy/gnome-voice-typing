#!/usr/bin/env bash
# Checks the first-launch dependency check (voice-typing-models deps) without installing anything:
#   * src/data/sysdeps.json lists the same packages as install.sh for dnf, apt and pacman
#   * `deps --json` reports the expected fields, and "Not now" (dismiss / undismiss) works
#   * with no Vulkan GPU (GVT_FAKE_GPU=0) the graphics-card packages are never asked for
set -uo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PY="${PYTHON:-python3}"; T="$(mktemp -d /tmp/gvt-deps.XXXXXX)"; trap 'rm -rf "$T"' EXIT
export XDG_CONFIG_HOME="$T/config"
PASS=0; FAILN=0
chk() { if [ "$2" = 0 ]; then PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; else FAILN=$((FAILN+1)); printf 'FAIL  %s\n' "$1"; fi; }
"$PY" - "$SRC" <<'PYE'; chk "sysdeps.json matches install.sh package lists" $?
import json, re, sys
src = sys.argv[1]; sh = open(src + '/install.sh').read(); d = json.load(open(src + '/src/data/sysdeps.json'))
bad = []
for pm in ('dnf', 'apt', 'pacman'):
    line = re.search(r'^\s*%s\)\s+RUNTIME="([^"]*)"; BUILD="([^"]*)"; GPUPKG="([^"]*)"' % pm, sh, re.M)
    if not line: bad.append(pm + ': not found in install.sh'); continue
    runtime, build, gpupkg = (x.split() for x in line.groups())
    if set(d[pm]['gpu']) != set(build + gpupkg): bad.append('%s gpu: %s vs %s' % (pm, sorted(d[pm]['gpu']), sorted(build + gpupkg)))
    if not set(d[pm]['typing']) <= set(runtime): bad.append(pm + ' typing not in RUNTIME')
print('\n'.join(bad)); sys.exit(1 if bad else 0)
PYE
J="$("$PY" "$SRC/src/bin/models_tool.py" deps --json)"; chk "deps --json runs" $?
echo "$J" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert all(k in d for k in ("needed","missing","missing_gpu","missing_typing","command","manual","pkexec","gpu","engine_built","dismissed"))'; chk "deps --json has every field" $?
echo "$J" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert not d["missing"] or d["command"][0]=="pkexec"'; chk "install command goes through pkexec (never silent sudo)" $?
"$PY" "$SRC/src/bin/models_tool.py" deps dismiss && "$PY" "$SRC/src/bin/models_tool.py" deps --json | "$PY" -c 'import json,sys; assert json.load(sys.stdin)["dismissed"]'; chk "Not now is remembered" $?
"$PY" "$SRC/src/bin/models_tool.py" deps undismiss && "$PY" "$SRC/src/bin/models_tool.py" deps --json | "$PY" -c 'import json,sys; assert not json.load(sys.stdin)["dismissed"]'; chk "Set up brings the prompt back" $?
GVT_FAKE_GPU=0 "$PY" "$SRC/src/bin/models_tool.py" deps --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d["missing_gpu"]==[]'; chk "no Vulkan GPU: graphics-card packages not requested" $?
echo; echo "$PASS passed, $FAILN failed"; [ "$FAILN" = 0 ]
