#!/usr/bin/env bash
# Repeatedly switch visualization writers while one 1080p compositor stays live.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-/tmp/ambient-s8-stress-$USER-$$}"
PY="${PY:-python3}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-30}"
LAYER_WIDTH="${LAYER_WIDTH:-1280}"
LAYER_HEIGHT="${LAYER_HEIGHT:-720}"
LAYER_FPS="${LAYER_FPS:-30}"
SWITCHES="${SWITCHES:-20}"
INTERVAL="${INTERVAL:-1}"
TAIL_SECONDS="${TAIL_SECONDS:-8}"
DURATION="$((2 + SWITCHES * INTERVAL + INTERVAL + TAIL_SECONDS))"
mkdir -p "$WORK"

VIZ_SOCKET="$WORK/visualization.sock"
LAYER_FIFO="$WORK/layer.raw"
mkfifo "$LAYER_FIFO"

MAIN_PID=""
KEEPER_PID=""
CHILD_PID=""
cleanup() {
  for pid in "$CHILD_PID" "$MAIN_PID" "$KEEPER_PID"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

timer() {
  ffmpeg -nostdin -hide_banner -loglevel error -re \
    -f lavfi -i anullsrc=r=8000:cl=mono -t "$1" -f null - </dev/null
}

progress_value() {
  awk -F= -v key="$2" '$1 == key {value=$2} END {gsub(/ /, "", value); print value}' "$1"
}

assert_live() {
  local phase="$1"
  kill -0 "$MAIN_PID" 2>/dev/null || {
    printf 'FAIL: main compositor died at %s\n' "$phase" >&2
    exit 1
  }
  local frame
  frame="$(progress_value "$WORK/main.progress" frame)"
  printf '%s pid=%s frame=%s\n' "$phase" "$MAIN_PID" "${frame:-0}" \
    >> "$WORK/switches.log"
}

start_plugin() {
  local side="$1" x
  if [[ "$side" == a ]]; then x=$((LAYER_WIDTH / 8)); else x=$((LAYER_WIDTH * 5 / 8)); fi
  ffmpeg -nostdin -hide_banner -loglevel error -re \
    -f lavfi -i "color=black:s=${LAYER_WIDTH}x${LAYER_HEIGHT}:r=${LAYER_FPS},drawbox=x=${x}:y=$((LAYER_HEIGHT / 4)):w=$((LAYER_WIDTH / 4)):h=$((LAYER_HEIGHT / 3)):color=white:t=fill" \
    -pix_fmt yuv420p -f rawvideo -y "unix://${VIZ_SOCKET}" </dev/null \
    > /dev/null 2>> "$WORK/children.log" &
  CHILD_PID=$!
}

stop_plugin() {
  if [[ -n "$CHILD_PID" ]]; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
  fi
}

"$PY" "$HERE/framekeeper.py" \
  --input "$VIZ_SOCKET" --width "$LAYER_WIDTH" --height "$LAYER_HEIGHT" \
  --fps "$LAYER_FPS" --stale-seconds 0.35 --status "$WORK/framekeeper.json" \
  > "$LAYER_FIFO" 2> "$WORK/framekeeper.log" &
KEEPER_PID=$!

ffmpeg -nostdin -hide_banner -loglevel error -stats \
  -progress "$WORK/main.progress" \
  -re -f lavfi -i "color=0x202040:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=${DURATION}" \
  -f rawvideo -pixel_format yuv420p -video_size "${LAYER_WIDTH}x${LAYER_HEIGHT}" \
  -framerate "$LAYER_FPS" -i "$LAYER_FIFO" \
  -filter_complex "[1:v]scale=${WIDTH}:${HEIGHT}:flags=fast_bilinear,fps=${FPS},split=2[vizc][vizm];[vizm]format=gray,lut=y='val*0.75'[alpha];[vizc][alpha]alphamerge[vizrgba];[0:v][vizrgba]overlay=eof_action=pass:format=auto,format=yuv420p[out]" \
  -map "[out]" -t "$DURATION" -c:v ffv1 -level 3 -y "$WORK/output.mkv" \
  </dev/null 2> "$WORK/main.log" &
MAIN_PID=$!
START_PID="$MAIN_PID"

timer 2
previous_frame=0
for ((switch=1; switch<=SWITCHES; switch++)); do
  stop_plugin
  case $((switch % 3)) in
    0) mode=null ;;
    1) mode=a; start_plugin a ;;
    2) mode=b; start_plugin b ;;
  esac
  timer "$INTERVAL"
  assert_live "switch-$switch-$mode"
  frame="$(progress_value "$WORK/main.progress" frame)"
  [[ -z "$frame" || "$frame" -gt "$previous_frame" ]] || {
    printf 'FAIL: progress did not advance at switch %s (%s -> %s)\n' \
      "$switch" "$previous_frame" "$frame" >&2
    exit 1
  }
  [[ -n "$frame" ]] && previous_frame="$frame"
  if (( switch == SWITCHES / 2 )) && [[ -n "$CHILD_PID" ]]; then
    kill -KILL "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    timer "$INTERVAL"
    assert_live forced-child-kill
    frame="$(progress_value "$WORK/main.progress" frame)"
    [[ -n "$frame" && "$frame" -gt "$previous_frame" ]] || {
      printf 'FAIL: progress did not advance after forced kill (%s -> %s)\n' \
        "$previous_frame" "${frame:-0}" >&2
      exit 1
    }
    previous_frame="$frame"
  fi
done
stop_plugin

wait "$MAIN_PID"
MAIN_PID=""
kill -TERM "$KEEPER_PID" 2>/dev/null || true
wait "$KEEPER_PID" || true
KEEPER_PID=""

frames="$(progress_value "$WORK/main.progress" frame)"
drop="$(progress_value "$WORK/main.progress" drop_frames)"
dup="$(progress_value "$WORK/main.progress" dup_frames)"
speed="$(progress_value "$WORK/main.progress" speed)"
expected="$((DURATION * FPS))"
[[ "$frames" == "$expected" ]] || { echo "FAIL: frames $frames != $expected" >&2; exit 1; }
[[ "$drop" == 0 && "$dup" == 0 ]] || { echo "FAIL: drop=$drop dup=$dup" >&2; exit 1; }
unique_pids="$(sed -n 's/.* pid=\([0-9][0-9]*\).*/\1/p' "$WORK/switches.log" | sort -u)"
[[ "$unique_pids" == "$START_PID" ]] || {
  printf 'FAIL: main PID changed; expected %s, observed %s\n' \
    "$START_PID" "$unique_pids" >&2
  exit 1
}
reconnects="$($PY -c "import json; print(json.load(open('$WORK/framekeeper.json'))['reconnects'])")"
expected_writers="$((SWITCHES - SWITCHES / 3))"
[[ "$reconnects" == "$expected_writers" ]] || {
  printf 'FAIL: accepted %s writers, expected %s\n' \
    "$reconnects" "$expected_writers" >&2
  exit 1
}

printf 'PASS: %s live switches, %s frames, speed=%s, drop=%s, dup=%s, main PID=%s, writers=%s\n' \
  "$SWITCHES" "$frames" "$speed" "$drop" "$dup" "$START_PID" "$reconnects"
printf 'artifacts: %s\n' "$WORK"