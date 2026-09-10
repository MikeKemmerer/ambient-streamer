#!/usr/bin/env bash
# Shared helpers for spike S2. Source this; it installs the cleanup trap.
set -euo pipefail

WORK="${WORK:-/tmp/s2spike}"
IMAGES="$WORK/images"
OUT="$WORK/out"
LOGS="${LOGS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$WORK" "$IMAGES" "$OUT" "$LOGS"

C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_0=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf '%s PASS%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s WARN%s %s\n' "$C_Y" "$C_0" "$*"; }
fail() { printf '%s FAIL%s %s\n' "$C_R" "$C_0" "$*"; }
die()  { fail "$*"; exit 1; }

TRACKED=()
track() { TRACKED+=("$1"); }

cleanup() {
  local rc=$?
  set +e
  for pid in "${TRACKED[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -TERM "$pid" 2>/dev/null
  done
  # Give TERM a moment, then be unambiguous about it.
  for _ in 1 2 3 4 5 6; do
    local alive=0
    for pid in "${TRACKED[@]:-}"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && alive=1
    done
    [[ $alive -eq 0 ]] && break
    sleep 0.25
  done
  for pid in "${TRACKED[@]:-}"; do
    [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null
  done
  # Belt and braces. The marker must be specific enough that it cannot match the
  # invoking shell's own command line, or cleanup kills its own session.
  pkill -f 's2spike-t[0-9]' 2>/dev/null
  pkill -f 's2spike/images' 2>/dev/null
  wait 2>/dev/null
  return $rc
}
trap cleanup EXIT INT TERM

# start_producer <name> <fps> <w> <h> <fmt> [extra args...]  -> echoes fifo path
YT_ENC=(-c:v libx264 -preset veryfast -fps_mode cfr
        -g 60 -keyint_min 60 -sc_threshold 0
        -x264-params "nal-hrd=cbr:force-cfr=1" -pix_fmt yuv420p)

progress_field() { # <progress file> <key>  -> last value
  awk -F= -v k="$2" '$1==k{v=$2} END{print v}' "$1" 2>/dev/null
}

require_ffmpeg_filters() {
  local missing=()
  for f in "$@"; do
    ffmpeg -hide_banner -filters 2>/dev/null | awk '{print $2}' | grep -qx "$f" || missing+=("$f")
  done
  [[ ${#missing[@]} -eq 0 ]] || die "missing ffmpeg filters: ${missing[*]}"
}
