#!/usr/bin/env bash
# Restartable visualization child: Icecast audio -> one plugin -> framekeeper.
set -euo pipefail
umask 077

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s visualizer %s\n' "$BLU" "$NC" "$*" >&2; }
ok()   { printf '%s[ok]%s visualizer %s\n' "$GRN" "$NC" "$*" >&2; }
warn() { printf '%s[!!]%s visualizer %s\n' "$YLW" "$NC" "$*" >&2; }
die()  { printf '%s[XX]%s visualizer %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

CHANNEL_NAME="${CHANNEL_NAME:-}"
[[ "$CHANNEL_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] \
  || die "CHANNEL_NAME must contain only lowercase letters, digits and hyphens"
PLUGIN_NAME="${PLUGIN_NAME:-${ACTIVE_PLUGIN:-}}"
[[ "$PLUGIN_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "exactly one valid PLUGIN_NAME is required"

WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-30}"
for dimension in WIDTH HEIGHT FPS; do
  value="${!dimension}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$dimension must be a positive integer"
done
(( WIDTH % 2 == 0 && HEIGHT % 2 == 0 )) \
  || die "WIDTH and HEIGHT must be even for yuv420p"
LAYER_WIDTH=$(( WIDTH < 1280 ? WIDTH : 1280 ))
LAYER_HEIGHT=$(( HEIGHT < 720 ? HEIGHT : 720 ))
LAYER_FPS=$(( FPS < 30 ? FPS : 30 ))

ICECAST_HOST="${ICECAST_HOST:-icecast}"
ICECAST_PORT="${ICECAST_PORT:-8081}"
CHANNEL_MOUNT="${CHANNEL_MOUNT:-/${CHANNEL_NAME}}"
AUDIO_URL="${AUDIO_URL:-http://${ICECAST_HOST}:${ICECAST_PORT}${CHANNEL_MOUNT}}"
ACCENT="${ACCENT:-#4FC3F7}"
ACCENT_FF="0x${ACCENT#\#}"
PLUGIN_PARAMS="${PLUGIN_PARAMS:-{\}}"
PLUGIN_DIR="${PLUGIN_DIR:-/plugins}"

RUN_ROOT="${RUN_ROOT:-/run/ambient}"
[[ "$RUN_ROOT" == /* && ! -L "$RUN_ROOT" ]] || die "RUN_ROOT must be an absolute real directory"
EXPECTED_RUN_DIR="${RUN_ROOT%/}/${CHANNEL_NAME}"
RUN_DIR="${RUN_DIR:-$EXPECTED_RUN_DIR}"
[[ "$RUN_DIR" == "$EXPECTED_RUN_DIR" ]] || die "RUN_DIR must be $EXPECTED_RUN_DIR"
[[ -d "$RUN_DIR" && ! -L "$RUN_DIR" ]] || die "RUN_DIR must be a real directory"
VIZ_SOCKET="${RUN_DIR}/visualization.sock"
GRAPH_FILE="${RUN_DIR}/visualizer-${$}.ffmpeg"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PLUGIN_PARAMS_BIN="${PLUGIN_PARAMS_BIN:-$(dirname "$0")/plugin_params.py}"
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-level+warning}"
FFMPEG_PID=""

stop_ffmpeg() {
  local pid="$1"
  local attempt
  kill -INT "$pid" 2>/dev/null || true
  for attempt in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || {
      wait "$pid" 2>/dev/null || true
      return
    }
    sleep 0.1
  done
  warn "ffmpeg did not stop after SIGINT; sending SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$FFMPEG_PID" ]]; then
    stop_ffmpeg "$FFMPEG_PID"
  fi
  [[ -f "$GRAPH_FILE" ]] && unlink "$GRAPH_FILE" 2>/dev/null || true
  log "cleaned up (rc=$rc)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

SOURCE_REVISION="${SOURCE_REVISION:-${IMAGE_REVISION:-unknown}}"
[[ "$SOURCE_REVISION" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || SOURCE_REVISION=unknown
ok "runtime source=ffmpeg/visualizer-entrypoint.sh revision=$SOURCE_REVISION"

FRAGMENT="${PLUGIN_DIR}/${PLUGIN_NAME}/viz.ffmpeg"
MANIFEST="${PLUGIN_DIR}/${PLUGIN_NAME}/config.json"
[[ -f "$FRAGMENT" ]] || die "plugin '$PLUGIN_NAME' has no viz.ffmpeg"
[[ -f "$MANIFEST" ]] || die "plugin '$PLUGIN_NAME' has no config.json"

MANIFEST_NAME="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("name", ""))' "$MANIFEST")"
[[ "$MANIFEST_NAME" == "$PLUGIN_NAME" ]] || die "plugin manifest name does not match directory"
MANIFEST_VERSION="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", "unknown"))' "$MANIFEST")"
[[ "$MANIFEST_VERSION" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || MANIFEST_VERSION=unknown
ok "plugin source=${PLUGIN_NAME}/viz.ffmpeg revision=$MANIFEST_VERSION"
grep -q '\${WIDTH}' "$FRAGMENT" && grep -q '\${HEIGHT}' "$FRAGMENT" \
  || die "plugin '$PLUGIN_NAME' does not derive its exact output size from the capped geometry"
[[ "$(grep -o '\[0:a\]' "$FRAGMENT" | wc -l)" -eq 1 ]] \
  || die "plugin '$PLUGIN_NAME' must have exactly one [0:a] input"
[[ "$(grep -o '\[\${OUT}\]' "$FRAGMENT" | wc -l)" -eq 1 ]] \
  || die "plugin '$PLUGIN_NAME' must have exactly one [\${OUT}] output"

AVAILABLE_FILTERS="$("$FFMPEG_BIN" -hide_banner -loglevel error -filters </dev/null | awk '{print $2}')"
while read -r required; do
  [[ -n "$required" ]] || continue
  grep -qx "$required" <<<"$AVAILABLE_FILTERS" \
    || die "plugin '$PLUGIN_NAME' requires filter '$required', absent from this build"
done < <("$PYTHON_BIN" -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1], encoding="utf-8")).get("requires_filters", [])))' "$MANIFEST")

BODY="$(tr '\n' ' ' < "$FRAGMENT" | sed 's/  */ /g; s/^ //; s/ $//')"
BODY="${BODY//\$\{WIDTH\}/$LAYER_WIDTH}"
BODY="${BODY//\$\{HEIGHT\}/$LAYER_HEIGHT}"
BODY="${BODY//\$\{FPS\}/$LAYER_FPS}"
BODY="${BODY//\$\{ACCENT\}/$ACCENT_FF}"
BODY="${BODY//\$\{OUT\}/vizout}"
while IFS='=' read -r token value; do
  [[ -n "$token" ]] || continue
  BODY="${BODY//\$\{$token\}/$value}"
done < <("$PYTHON_BIN" "$PLUGIN_PARAMS_BIN" "$MANIFEST" "$PLUGIN_PARAMS")
if grep -q '\${' <<<"$BODY"; then
  die "plugin '$PLUGIN_NAME' has unsubstituted parameters"
fi
printf '%s\n' "$BODY" > "$GRAPH_FILE"

for _ in $(seq 1 150); do
  [[ -S "$VIZ_SOCKET" ]] && break
  sleep 0.1
done
[[ -S "$VIZ_SOCKET" ]] || die "framekeeper socket did not become ready"

ok "plugin=$PLUGIN_NAME layer=${LAYER_WIDTH}x${LAYER_HEIGHT}@${LAYER_FPS} socket=$VIZ_SOCKET"
"$FFMPEG_BIN" -nostdin -hide_banner -loglevel "$FFMPEG_LOGLEVEL" \
  -probesize 32k -analyzeduration 500000 -fflags nobuffer \
  -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 \
  -reconnect_delay_max 5 \
  -i "$AUDIO_URL" \
  -filter_complex_script "$GRAPH_FILE" \
  -map '[vizout]' -an -c:v rawvideo -pix_fmt yuv420p \
  -f rawvideo -y "unix://${VIZ_SOCKET}" \
  </dev/null &
FFMPEG_PID=$!
wait "$FFMPEG_PID"