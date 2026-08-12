#!/usr/bin/env bash
# Shared helpers for the S5 MediaMTX relay spike.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="$SPIKE_DIR/results"
WORK_DIR="$SPIKE_DIR/work"

PROJECT="ambient-spike"
MTX_IMAGE="bluenviron/mediamtx:1.9.3"

RELAY_RTMP="rtmp://127.0.0.1:1935"
RELAY_HLS="http://127.0.0.1:8888"
RELAY_API="http://127.0.0.1:9000"
YT_RTMP="rtmp://127.0.0.1:8081"
YT_API="http://127.0.0.1:8090"

mkdir -p "$RESULTS_DIR" "$WORK_DIR"

if [[ -t 1 ]]; then
  _C_RED=$'\e[31m'; _C_GRN=$'\e[32m'; _C_YEL=$'\e[33m'; _C_BLU=$'\e[34m'; _C_OFF=$'\e[0m'
else
  _C_RED=""; _C_GRN=""; _C_YEL=""; _C_BLU=""; _C_OFF=""
fi

log()  { printf '%s[ .. ]%s %s\n' "$_C_BLU" "$_C_OFF" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$_C_GRN" "$_C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$_C_YEL" "$_C_OFF" "$*"; }
die()  { printf '%s[fail]%s %s\n' "$_C_RED" "$_C_OFF" "$*" >&2; exit 1; }

pass_fail() { # pass_fail <PASS|FAIL> <question> <detail>
  printf '%s :: %s :: %s\n' "$1" "$2" "$3" | tee -a "$RESULTS_DIR/verdicts.txt"
}

now_ms() { date +%s%3N; }

http_get() { # http_get <url>  -> body on stdout, non-zero on error
  python3 - "$1" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
        sys.stdout.write(r.read().decode("utf-8", "replace"))
except Exception as exc:
    sys.stderr.write(f"{exc}\n")
    sys.exit(1)
PY
}

http_code() { # http_code <url> -> HTTP status (or 000)
  python3 - "$1" <<'PY'
import sys, urllib.request, urllib.error
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("000")
PY
}

# Every background process started by a test is tracked so cleanup is unconditional.
SPIKE_PIDS=()
track() { SPIKE_PIDS+=("$1"); }

kill_tracked() {
  local pid
  for pid in "${SPIKE_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in "${SPIKE_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
  done
  SPIKE_PIDS=()
}

# Only ever touches processes this spike started and the ambient-spike compose project.
# Containers are torn down here too: an interrupted spike must not leave relays running on
# a shared host.
spike_cleanup() {
  kill_tracked
  pkill -f 'ffmpeg.*ambient-spike-tag' 2>/dev/null || true
  pkill -f 'poll-path.py' 2>/dev/null || true
  docker rm -f ambient-spike-cfgcheck ambient-spike-neg >/dev/null 2>&1 || true
  relay_down
}

# Installed at source time so no script can forget it, including on Ctrl-C or SIGTERM.
trap spike_cleanup EXIT INT TERM

relay_up() { # relay_up [config-file-relative-to-spike-dir]
  local cfg="${1:-../../docker/mediamtx.yml}"
  RELAY_CONFIG="$cfg" docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" up -d
  local i
  for i in $(seq 1 40); do
    [[ "$(http_code "$RELAY_API/v3/paths/list")" == "200" ]] && { ok "relay API up"; return 0; }
    sleep 0.25
  done
  docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" logs --no-color relay | tail -30
  die "relay API never came up"
}

relay_down() {
  docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" down -v --remove-orphans 2>/dev/null || true
}

# 1080p program bed + 480p preview bed, encoded once and republished with -c copy so
# publisher CPU does not pollute relay measurements.
ensure_beds() {
  if [[ ! -f "$WORK_DIR/bed.mp4" ]]; then
    log "encoding 1080p30 6Mbps program bed (30s)"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
      -f lavfi -i "testsrc2=size=1920x1080:rate=30" \
      -f lavfi -i "sine=frequency=440:sample_rate=48000" \
      -t 30 -c:v libx264 -preset veryfast -b:v 6000k -maxrate 6000k -bufsize 12000k \
      -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p \
      -c:a aac -b:a 128k -ar 48000 -ac 2 \
      "$WORK_DIR/bed.mp4" </dev/null
  fi
  if [[ ! -f "$WORK_DIR/bed-low.mp4" ]]; then
    log "encoding 854x480 800kbps preview bed (30s)"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
      -f lavfi -i "testsrc2=size=854x480:rate=30" \
      -f lavfi -i "sine=frequency=220:sample_rate=48000" \
      -t 30 -c:v libx264 -preset veryfast -b:v 800k -maxrate 800k -bufsize 1600k \
      -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p \
      -c:a aac -b:a 96k -ar 48000 -ac 2 \
      "$WORK_DIR/bed-low.mp4" </dev/null
  fi
  ok "beds ready"
}

publish_bed() { # publish_bed <bed-file> <rtmp-url> <logfile> -> echoes pid
  ffmpeg -nostdin -hide_banner -loglevel warning -re -stream_loop -1 \
    -i "$1" -c copy -f flv -metadata comment=ambient-spike-tag "$2" \
    </dev/null >"$3" 2>&1 &
  echo $!
}

read_stream() { # read_stream <rtmp-url> <logfile> -> echoes pid
  ffmpeg -nostdin -hide_banner -loglevel warning -i "$1" \
    -c copy -f null -metadata comment=ambient-spike-tag - \
    </dev/null >"$2" 2>&1 &
  echo $!
}

relay_stats() { # relay_stats <label> -> "label cpu% memMiB"
  local raw
  raw="$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' "${PROJECT}-relay-1" 2>/dev/null || echo 'n/a n/a')"
  printf '%-24s %s\n' "$1" "$raw"
}
