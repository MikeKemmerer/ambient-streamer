#!/usr/bin/env bash
# Shared config and helpers for spike S1.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_DIR="${MEDIA_DIR:-$SPIKE_DIR/media}"
OUT_DIR="${OUT_DIR:-$SPIKE_DIR/out}"
LIQ_IMAGE="${LIQ_IMAGE:-savonet/liquidsoap:v2.4.2}"
# High port: 8090/8888/9000/8081 are held by the concurrent s5-mediamtx-relay spike.
HARBOR_PORT="${HARBOR_PORT:-18090}"
HARBOR_MOUNT="${HARBOR_MOUNT:-live}"
CONTAINER_PREFIX="s1-"

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s %s\n' "$BLU" "$NC" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s[!!]%s %s\n' "$YLW" "$NC" "$*"; }
die()  { printf '%s[XX]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

now() { date +%s.%N; }

# Every ffmpeg call in this spike goes through here: -nostdin AND </dev/null are
# both required or a backgrounded ffmpeg gets SIGTTIN-stopped.
ff() { ffmpeg -nostdin -hide_banner "$@" </dev/null; }

cleanup_containers() {
  local ids
  ids="$(docker ps -aq --filter "name=^${CONTAINER_PREFIX}" || true)"
  [[ -n "$ids" ]] && docker rm -f $ids >/dev/null 2>&1 || true
}

# Targeted removal: several spike scripts may run at once on different ports.
rm_ctr() {
  local s
  for s in "$@"; do docker rm -f "${CONTAINER_PREFIX}${s}" >/dev/null 2>&1 || true; done
}

kill_pid() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -INT "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.25; done
  kill -9 "$pid" 2>/dev/null || true
}

alive() { kill -0 "${1:-0}" 2>/dev/null; }

# out_time_ms from an -progress file; empty if ffmpeg has not emitted one yet.
progress_out_ms() {
  grep -a '^out_time_ms=' "$1" 2>/dev/null | tail -1 | cut -d= -f2 || true
}

liq_run() {
  # liq_run <container-suffix> <script.liq> [extra docker args...]
  local name="$1"; shift
  local script="$1"; shift
  docker run -d --name "${CONTAINER_PREFIX}${name}" \
    -v "$SPIKE_DIR:/spike" \
    -v "$MEDIA_DIR:/media" \
    -e MUSIC_DIR=/media \
    -e HARBOR_PORT="$HARBOR_PORT" \
    -e HARBOR_MOUNT="$HARBOR_MOUNT" \
    "$@" \
    "$LIQ_IMAGE" liquidsoap "$script" >/dev/null
}

require_media() {
  [[ -d "$MEDIA_DIR" ]] && compgen -G "$MEDIA_DIR/*.mp3" >/dev/null \
    || die "no test media in $MEDIA_DIR — run 00-setup.sh first"
}

# Docker publishes the port before Liquidsoap registers the mount handler, so a
# TCP connect is a false ready signal. Only an HTTP 200 on the mount counts.
harbor_ready() {
  python3 - "${1:-$HARBOR_PORT}" "${2:-$HARBOR_MOUNT}" <<'PY'
import socket, sys
port, mount = int(sys.argv[1]), sys.argv[2]
try:
    s = socket.create_connection(("127.0.0.1", port), 2)
    s.settimeout(2)
    s.sendall(f"GET /{mount} HTTP/1.0\r\nUser-Agent: readiness\r\n\r\n".encode())
    ok = b"200 OK" in s.recv(128)
    s.close()
except OSError:
    ok = False
sys.exit(0 if ok else 1)
PY
}

wait_harbor() {
  local tries="${1:-60}"
  for _ in $(seq 1 "$tries"); do
    harbor_ready && return 0
    sleep 0.5
  done
  return 1
}

mkdir -p "$OUT_DIR"
