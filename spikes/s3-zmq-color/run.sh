#!/usr/bin/env bash
# S3 - prove filter parameters can be mutated on a RUNNING FFmpeg graph over ZMQ.
#
# Every phase records lossless video while a Python controller sends commands,
# then compares pixel values before and after each command.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$HERE/work}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
export PYTHONPATH="$ROOT/lib"

RES="320x180"
FPS=30
SRC="color=c=0x3060C0:s=${RES}:r=${FPS}"
PHASES="${PHASES:-p0 p1 p2 p3 p3b p3c p4 p5a p5b}"

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '\033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\033[31mdie\033[0m  %s\n' "$*" >&2; exit 1; }
want() { [[ " $PHASES " == *" $1 "* ]]; }

mkdir -p "$WORK/graphs"

# This host also runs Plex; only ever kill PIDs started by this script.
FFPID=""
cleanup() { if [[ -n "$FFPID" ]]; then kill "$FFPID" 2>/dev/null || true; fi; }
trap cleanup EXIT

mk_graph() { # <template> <out> <port> [eval]
  sed -e "s/__PORT__/$3/g" -e "s/__EVAL__/${4:-frame}/g" "$1" > "$2"
}

start_capture() { # <graph> <duration> <outfile>
  ffmpeg -nostdin -hide_banner -loglevel error -nostats \
    -re -f lavfi -i "$SRC" \
    -filter_complex_script "$1" -map "[v]" \
    -c:v ffv1 -level 3 -t "$2" -y "$3" </dev/null &
  FFPID=$!
}

start_null() { # <graph> <duration>
  ffmpeg -nostdin -hide_banner -loglevel error -nostats \
    -re -f lavfi -i "$SRC" \
    -filter_complex_script "$1" -map "[v]" \
    -c:v wrapped_avframe -t "$2" -f null - </dev/null &
  FFPID=$!
}

finish_capture() {
  local rc=0
  wait "$FFPID" || rc=$?
  FFPID=""
  return "$rc"
}

########################################################################
if want p0; then
log "P0  filter instance names as FFmpeg sees them"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p0.txt" 5560 frame
ffmpeg -nostdin -hide_banner -loglevel debug -nostats \
  -f lavfi -i "$SRC" -filter_complex_script "$WORK/graphs/p0.txt" \
  -map "[v]" -c:v wrapped_avframe -t 0.2 -f null - </dev/null 2>&1 \
  | grep -oE '^\[[^]]+ @ 0x[0-9a-f]+\]' \
  | sed -e 's/^\[//' -e 's/ @ 0x[0-9a-f]*\]$//' | sort -u \
  > "$WORK/p0_names.txt" || true
cat "$WORK/p0_names.txt"
fi

########################################################################
if want p1; then
log "P1  discrete parameter steps, correlated against pixels"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p1.txt" 5561 frame
start_capture "$WORK/graphs/p1.txt" 18 "$WORK/p1_steps.mkv"
"$PY" "$HERE/s3_control.py" steps --addr tcp://127.0.0.1:5561 \
  --out "$WORK/p1_commands.json"
finish_capture || warn "ffmpeg exited non-zero"
"$PY" "$HERE/analyze_s3.py" steps "$WORK/p1_steps.mkv" "$WORK/p1_commands.json" \
  | tee "$WORK/p1_analysis.txt"
fi

########################################################################
if want p2; then
log "P2  application latency: 15 steps spaced exactly 100 ms apart"
mk_graph "$HERE/graphs/eq_chain.tmpl" "$WORK/graphs/p2.txt" 5562
start_capture "$WORK/graphs/p2.txt" 6 "$WORK/p2_latency.mkv"
"$PY" "$HERE/s3_control.py" latency --addr tcp://127.0.0.1:5562 \
  --out "$WORK/p2_commands.json" --steps 15 --interval 0.100
finish_capture || warn "ffmpeg exited non-zero"
"$PY" "$HERE/analyze_s3.py" latency "$WORK/p2_latency.mkv" "$WORK/p2_commands.json" \
  | tee "$WORK/p2_analysis.txt"
fi

########################################################################
if want p3; then
log "P3  reply semantics and addressing boundary"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p3.txt" 5563 frame
start_null "$WORK/graphs/p3.txt" 20
"$PY" "$HERE/s3_control.py" replies --addr tcp://127.0.0.1:5563 \
  --out "$WORK/p3_commands.json" | tee "$WORK/p3_analysis.txt"
rc=0; finish_capture || rc=$?
if [[ $rc -eq 0 ]]; then ok "graph survived every reply probe"
else warn "GRAPH DIED during the reply probes (exit $rc)"; fi
fi

########################################################################
if want p3b; then
log "P3b malformed messages, one FFmpeg process per message"
: > "$WORK/p3b_analysis.txt"
port=5580
for case in target_only empty_message whitespace_only target_and_space \
            arg_with_trailing_token quoted_arg_with_space unknown_target_only; do
  port=$((port + 1))
  mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p3b_$case.txt" "$port" frame
  start_null "$WORK/graphs/p3b_$case.txt" 8
  set +e
  "$PY" "$HERE/s3_control.py" crashtest --addr "tcp://127.0.0.1:$port" \
    --out "$WORK/p3b_$case.json" --case "$case" | tee -a "$WORK/p3b_analysis.txt"
  rc=0; wait "$FFPID"; rc=$?; FFPID=""
  set -e
  sig=""
  [[ $rc -gt 128 ]] && sig=" (killed by signal $((rc - 128)))"
  printf '  ffmpeg exit status for %-24s : %s%s\n' "$case" "$rc" "$sig" \
    | tee -a "$WORK/p3b_analysis.txt"
done
fi

########################################################################
if want p3c; then
log "P3c does one failed command permanently disable a filter instance?"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p3c.txt" 5567 frame
start_null "$WORK/graphs/p3c.txt" 20
"$PY" "$HERE/s3_control.py" poison --addr tcp://127.0.0.1:5567 \
  --out "$WORK/p3c_commands.json" | tee "$WORK/p3c_analysis.txt"
finish_capture || warn "ffmpeg exited non-zero"
fi

########################################################################
if want p4; then
log "P4  command throughput on a 30 fps graph"
mk_graph "$HERE/graphs/eq_chain.tmpl" "$WORK/graphs/p4.txt" 5564
start_null "$WORK/graphs/p4.txt" 14
"$PY" "$HERE/s3_control.py" throughput --addr tcp://127.0.0.1:5564 \
  --out "$WORK/p4_stats.json" --count 200 | tee "$WORK/p4_analysis.txt"
finish_capture || warn "ffmpeg exited non-zero"
fi

########################################################################
if want p5a; then
log "P5a ramping via a time expression, eq eval=frame"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p5a.txt" 5565 frame
start_capture "$WORK/graphs/p5a.txt" 14 "$WORK/p5a_ramp.mkv"
"$PY" "$HERE/s3_control.py" ramp --addr tcp://127.0.0.1:5565 \
  --out "$WORK/p5a_commands.json"
finish_capture || warn "ffmpeg exited non-zero"
"$PY" "$HERE/analyze_s3.py" ramp "$WORK/p5a_ramp.mkv" "$WORK/p5a_commands.json" \
  | tee "$WORK/p5a_analysis.txt"
fi

########################################################################
if want p5b; then
log "P5b same expressions, eq eval=init (control case)"
mk_graph "$HERE/graphs/color_chain.tmpl" "$WORK/graphs/p5b.txt" 5566 init
start_capture "$WORK/graphs/p5b.txt" 14 "$WORK/p5b_ramp.mkv"
"$PY" "$HERE/s3_control.py" ramp --addr tcp://127.0.0.1:5566 \
  --out "$WORK/p5b_commands.json"
finish_capture || warn "ffmpeg exited non-zero"
"$PY" "$HERE/analyze_s3.py" ramp "$WORK/p5b_ramp.mkv" "$WORK/p5b_commands.json" \
  | tee "$WORK/p5b_analysis.txt"
fi

ok "S3 phases [$PHASES] complete; artefacts in $WORK"
