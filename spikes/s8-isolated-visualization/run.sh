#!/usr/bin/env bash
# S8 - prove a stable compositor can survive restartable visualization writers.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-/tmp/ambient-s8-$USER-$$}"
PY="${PY:-python3}"
WIDTH="${WIDTH:-320}"
HEIGHT="${HEIGHT:-180}"
FPS="${FPS:-30}"
if (( WIDTH > 1280 || HEIGHT > 720 )); then
  LAYER_WIDTH="${LAYER_WIDTH:-1280}"
  LAYER_HEIGHT="${LAYER_HEIGHT:-720}"
else
  LAYER_WIDTH="${LAYER_WIDTH:-$WIDTH}"
  LAYER_HEIGHT="${LAYER_HEIGHT:-$HEIGHT}"
fi
LAYER_FPS="${LAYER_FPS:-$FPS}"
DURATION="${DURATION:-20}"
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

assert_main() {
  kill -0 "$MAIN_PID" 2>/dev/null || {
    printf 'main compositor died during %s\n' "$1" >&2
    exit 1
  }
  printf '%s %s\n' "$1" "$MAIN_PID" >> "$WORK/main-pids.txt"
}

run_plugin() {
  local name="$1" center_x="$2"
  local box_width=$((LAYER_WIDTH / 5))
  local box_height=$((LAYER_HEIGHT / 3))
  local box_x=$((center_x - box_width / 2))
  local box_y=$((LAYER_HEIGHT / 6))
  ffmpeg -nostdin -hide_banner -loglevel error -re \
    -f lavfi -i "color=black:s=${LAYER_WIDTH}x${LAYER_HEIGHT}:r=${LAYER_FPS},drawbox=x=${box_x}:y=${box_y}:w=${box_width}:h=${box_height}:color=white:t=fill" \
    -t 4 -pix_fmt yuv420p -f rawvideo -y "unix://${VIZ_SOCKET}" </dev/null &
  CHILD_PID=$!
  wait "$CHILD_PID"
  CHILD_PID=""
  assert_main "$name"
}

"$PY" "$HERE/framekeeper.py" \
  --input "$VIZ_SOCKET" --width "$LAYER_WIDTH" --height "$LAYER_HEIGHT" \
  --fps "$LAYER_FPS" \
  --stale-seconds 0.35 --status "$WORK/framekeeper.json" \
  > "$LAYER_FIFO" 2> "$WORK/framekeeper.log" &
KEEPER_PID=$!

ffmpeg -nostdin -hide_banner -loglevel error -stats \
  -progress "$WORK/main.progress" \
  -re -f lavfi -i "color=0x202040:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=${DURATION}" \
  -f rawvideo -pixel_format yuv420p -video_size "${LAYER_WIDTH}x${LAYER_HEIGHT}" \
  -framerate "$LAYER_FPS" \
  -i "$LAYER_FIFO" \
  -filter_complex "[1:v]scale=${WIDTH}:${HEIGHT}:flags=fast_bilinear,fps=${FPS},split=2[vizc][vizm];[vizm]format=gray,lut=y='val*0.75'[alpha];[vizc][alpha]alphamerge[vizrgba];[0:v][vizrgba]overlay=eof_action=pass:format=auto,format=yuv420p[out]" \
  -map "[out]" -t "$DURATION" -c:v ffv1 -level 3 -y "$WORK/output.mkv" \
  </dev/null 2> "$WORK/main.log" &
MAIN_PID=$!
printf 'start %s\n' "$MAIN_PID" > "$WORK/main-pids.txt"

timer 2
assert_main initial-null
run_plugin plugin-a $((LAYER_WIDTH / 4))
timer 2
assert_main restart-gap
run_plugin plugin-b $((LAYER_WIDTH * 3 / 4))
timer 4

wait "$MAIN_PID"
MAIN_PID=""
kill -TERM "$KEEPER_PID" 2>/dev/null || true
wait "$KEEPER_PID" || true
KEEPER_PID=""

frames="$(ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$WORK/output.mkv")"
expected="$((DURATION * FPS))"
[[ "$frames" == "$expected" ]] || {
  printf 'expected %s frames, got %s\n' "$expected" "$frames" >&2
  exit 1
}

unique_pids="$(awk '{print $2}' "$WORK/main-pids.txt" | sort -u | wc -l)"
[[ "$unique_pids" == 1 ]] || {
  printf 'main PID changed:\n' >&2
  cat "$WORK/main-pids.txt" >&2
  exit 1
}

"$PY" "$HERE/analyze.py" "$WORK/output.mkv" "$WORK/framekeeper.json" \
  --width "$WIDTH" --height "$HEIGHT" | tee "$WORK/analysis.json"
printf 'PASS: main PID %s stayed alive for %s frames; artifacts: %s\n' \
  "$(awk 'NR==1 {print $2}' "$WORK/main-pids.txt")" "$frames" "$WORK"