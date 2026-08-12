#!/usr/bin/env bash
# Probe how the filtergraph parser handles bind_address escaping and newlines.
# Throwaway diagnostic for S3; each case runs a 0.2 s null decode.
set -uo pipefail

WORK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/work/escape"
mkdir -p "$WORK"
SRC="color=c=0x3060C0:s=64x64:r=30"

try() { # <label> <graph-text>
  local label="$1" graph="$2"
  printf '%s' "$graph" > "$WORK/$label.txt"
  local out rc
  out=$(ffmpeg -nostdin -hide_banner -loglevel error -nostats \
    -f lavfi -i "$SRC" -filter_complex_script "$WORK/$label.txt" \
    -map "[v]" -c:v wrapped_avframe -t 0.2 -f null - </dev/null 2>&1)
  rc=$?
  if [[ $rc -eq 0 ]]; then
    printf 'PASS  %-28s %s\n' "$label" "$(cat "$WORK/$label.txt" | head -1)"
  else
    printf 'FAIL  %-28s %s\n' "$label" "$(printf '%s' "$out" | grep -m1 -iE 'error|invalid|no option' || printf 'rc=%d' "$rc")"
  fi
}

try single_backslash   '[0:v]zmq@ctl=bind_address=tcp\://127.0.0.1\:5591,null[v]'
try double_backslash   '[0:v]zmq@ctl=bind_address=tcp\\://127.0.0.1\\:5592,null[v]'
try quoted_single_bs   "[0:v]zmq@ctl=bind_address='tcp\\://127.0.0.1\\:5593',null[v]"
try quoted_plain       "[0:v]zmq@ctl=bind_address='tcp://127.0.0.1:5594',null[v]"
try quad_backslash     '[0:v]zmq@ctl=bind_address=tcp\\\\://127.0.0.1\\\\:5595,null[v]'
try shorthand_b        '[0:v]zmq@ctl=b=tcp\\://127.0.0.1\\:5596,null[v]'
try default_bind       '[0:v]zmq@ctl,null[v]'
try no_label           '[0:v]zmq=bind_address=tcp\\://127.0.0.1\\:5597,null[v]'

printf '\n-- newline handling in a filtergraph script --\n'
try oneline_chain      '[0:v]drawbox@box=x=1:y=1:w=8:h=8:color=red:t=fill,eq@eq=eval=frame:brightness=0.0,hue@hue=h=0:s=1,format=yuv420p[v]'
try multiline_chain    '[0:v]drawbox@box=x=1:y=1:w=8:h=8:color=red:t=fill,
eq@eq=eval=frame:brightness=0.0,
hue@hue=h=0:s=1,
format=yuv420p[v]'
