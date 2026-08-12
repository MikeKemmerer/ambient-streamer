#!/usr/bin/env bash
# S4 - prove visualization branches can be switched live with streamselect,
# and measure what idle branches cost.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$HERE/work}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
export PYTHONPATH="$ROOT/lib"

# Constant amplitude so no frame is ever black; wandering frequency so no two
# frames are accidentally identical.
ASRC='aevalsrc=0.4*sin(2*PI*(300+120*sin(2*PI*t/5))*t)|0.4*sin(2*PI*437*t):s=44100:c=stereo'
CPU_SECS="${CPU_SECS:-15}"
PHASES="${PHASES:-a b c d e f g h}"

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

mk_graph() { # <template> <out> <port> <w> <h>
  sed -e "s/__PORT__/$3/g" -e "s/__W__/$4/g" -e "s/__H__/$5/g" "$1" > "$2"
}

progress_val() { grep "^$2=" "$1" | tail -1 | cut -d= -f2- | tr -d ' '; }

cpu_run() { # <label> <graph> <output args...>
  local label="$1" graph="$2"; shift 2
  /usr/bin/time -o "$WORK/$label.time" \
    -f "cpu_pct=%P wall=%e user=%U sys=%S maxrss_kb=%M" \
    ffmpeg -nostdin -hide_banner -loglevel error -nostats \
      -progress "$WORK/$label.progress" \
      -re -f lavfi -i "$ASRC" \
      -filter_complex_script "$graph" \
      "$@" -t "$CPU_SECS" -f null - </dev/null
  printf '%-26s %s  speed=%s frames=%s drop=%s dup=%s\n' \
    "$label" "$(cat "$WORK/$label.time")" \
    "$(progress_val "$WORK/$label.progress" speed)" \
    "$(progress_val "$WORK/$label.progress" frame)" \
    "$(progress_val "$WORK/$label.progress" drop_frames)" \
    "$(progress_val "$WORK/$label.progress" dup_frames)" \
    | tee -a "$WORK/cpu_summary.txt"
}

########################################################################
if want a; then
log "A  preflight: audio source and filter availability"
ffmpeg -nostdin -hide_banner -loglevel error \
  -f lavfi -i "$ASRC" -t 0.5 -c:a pcm_s16le -y "$WORK/preflight.wav" </dev/null \
  && ok "audio source parses" || die "audio source rejected"
# Listed once into a file; `grep -q` on a pipe would SIGPIPE ffmpeg under pipefail.
ffmpeg -hide_banner -filters > "$WORK/filters.txt" 2>/dev/null || true
for f in streamselect astreamselect showwaves showfreqs avectorscope zmq azmq; do
  if grep -qE "[[:space:]]$f[[:space:]]" "$WORK/filters.txt"; then
    ok "filter present: $f"
  else
    die "filter missing: $f"
  fi
done
fi

########################################################################
if want b; then
log "B  live branch switching, proven from output pixels"
mk_graph "$HERE/graphs/switch3.tmpl" "$WORK/graphs/switch.txt" 5571 320 180
ffmpeg -nostdin -hide_banner -loglevel error -nostats \
  -progress "$WORK/switch.progress" \
  -re -f lavfi -i "$ASRC" \
  -filter_complex_script "$WORK/graphs/switch.txt" -map "[v]" \
  -c:v ffv1 -level 3 -t 12 -y "$WORK/switch.mkv" </dev/null &
FFPID=$!
"$PY" "$HERE/s4_control.py" video --addr tcp://127.0.0.1:5571 \
  --out "$WORK/switch_commands.json"
wait "$FFPID" || warn "ffmpeg exited non-zero"; FFPID=""
printf 'muxer counters: frames=%s drop=%s dup=%s speed=%s\n' \
  "$(progress_val "$WORK/switch.progress" frame)" \
  "$(progress_val "$WORK/switch.progress" drop_frames)" \
  "$(progress_val "$WORK/switch.progress" dup_frames)" \
  "$(progress_val "$WORK/switch.progress" speed)"
"$PY" "$HERE/analyze_s4.py" "$WORK/switch.mkv" "$WORK/switch_commands.json" \
  --duration 12 | tee "$WORK/switch_analysis.txt"
fi

########################################################################
if want c; then
log "C  streamselect error and addressing probes"
mk_graph "$HERE/graphs/switch3.tmpl" "$WORK/graphs/errors.txt" 5572 320 180
ffmpeg -nostdin -hide_banner -loglevel error -nostats \
  -re -f lavfi -i "$ASRC" \
  -filter_complex_script "$WORK/graphs/errors.txt" -map "[v]" \
  -c:v wrapped_avframe -t 8 -f null - </dev/null &
FFPID=$!
"$PY" "$HERE/s4_control.py" errors --addr tcp://127.0.0.1:5572 \
  --out "$WORK/error_probes.json" | tee "$WORK/error_analysis.txt"
wait "$FFPID" || warn "ffmpeg exited non-zero"; FFPID=""
fi

########################################################################
if want d; then
log "D  branch compatibility constraints (the mismatched branch is routed live)"
: > "$WORK/constraints.txt"
port=5590
for kind in res fps fmt sar; do
  port=$((port + 1))
  sed -e "s/__PORT__/$port/g" "$HERE/graphs/mismatch_$kind.tmpl" \
    > "$WORK/graphs/mismatch_$kind.txt"
  {
    printf '\n--- mismatch: %s ---\n' "$kind"
  } | tee -a "$WORK/constraints.txt"
  set +e
  ffmpeg -nostdin -hide_banner -loglevel warning -nostats \
    -re -f lavfi -i "$ASRC" \
    -filter_complex_script "$WORK/graphs/mismatch_$kind.txt" -map "[v]" \
    -c:v ffv1 -level 3 -t 5 -y "$WORK/mismatch_$kind.mkv" \
    </dev/null 2> "$WORK/mismatch_$kind.stderr" &
  FFPID=$!
  "$PY" "$HERE/s4_control.py" mismatch --addr "tcp://127.0.0.1:$port" \
    --out "$WORK/mismatch_$kind.json" >> "$WORK/constraints.txt" 2>&1
  rc=0; wait "$FFPID"; rc=$?; FFPID=""
  set -e
  {
    printf '  ffmpeg exit status: %s\n' "$rc"
    printf '  stderr: %s\n' "$(head -c 600 "$WORK/mismatch_$kind.stderr" | tr '\n' '|')"
    if [[ -s "$WORK/mismatch_$kind.mkv" ]]; then
      "$PY" "$HERE/analyze_mismatch.py" "$WORK/mismatch_$kind.mkv" \
        "$WORK/mismatch_$kind.json" 2>&1
    else
      printf '  no output file produced\n'
    fi
  } | tee -a "$WORK/constraints.txt"
done
fi

########################################################################
if want e; then
log "E  CPU cost of idle branches at 1280x720, ${CPU_SECS}s each"
: > "$WORK/cpu_summary.txt"
mk_graph "$HERE/graphs/viz1.tmpl" "$WORK/graphs/viz1_720.txt" 0 1280 720
mk_graph "$HERE/graphs/viz2.tmpl" "$WORK/graphs/viz2_720.txt" 0 1280 720
mk_graph "$HERE/graphs/viz3.tmpl" "$WORK/graphs/viz3_720.txt" 0 1280 720
printf '[0:a]anull[aout]\n' > "$WORK/graphs/viz0.txt"

cpu_run viz0_audio_only "$WORK/graphs/viz0.txt" -map "[aout]" -c:a pcm_s16le
cpu_run viz1_720_filteronly "$WORK/graphs/viz1_720.txt" -map "[v]" -c:v wrapped_avframe
cpu_run viz2_720_filteronly "$WORK/graphs/viz2_720.txt" -map "[v]" -c:v wrapped_avframe
cpu_run viz3_720_filteronly "$WORK/graphs/viz3_720.txt" -map "[v]" -c:v wrapped_avframe
fi

if want f; then
log "F  CPU cost with the real YouTube encoder settings, 720p CBR"
mk_graph "$HERE/graphs/encode3.tmpl" "$WORK/graphs/encode3_720.txt" 5573 1280 720
cpu_run viz3_720_x264 "$WORK/graphs/encode3_720.txt" \
  -map "[v]" -map "[aout]" \
  -c:v libx264 -preset veryfast -r 30 -fps_mode cfr \
  -b:v 3000k -minrate 3000k -maxrate 3000k -bufsize 6000k \
  -g 60 -keyint_min 60 -sc_threshold 0 \
  -x264-params "nal-hrd=cbr:force-cfr=1" -pix_fmt yuv420p \
  -c:a aac -b:a 128k -ar 44100
fi

if want g; then
log "G  CPU cost of 3 branches at 1920x1080 for scaling"
mk_graph "$HERE/graphs/viz3.tmpl" "$WORK/graphs/viz3_1080.txt" 0 1920 1080
cpu_run viz3_1080_filteronly "$WORK/graphs/viz3_1080.txt" -map "[v]" -c:v wrapped_avframe
fi

########################################################################
if want h; then
log "H  astreamselect: live audio branch switching"
mk_graph "$HERE/graphs/audio.tmpl" "$WORK/graphs/audio.txt" 5574 0 0
ffmpeg -nostdin -hide_banner -loglevel error -nostats \
  -re -f lavfi -i "sine=frequency=440:sample_rate=44100" \
  -re -f lavfi -i "sine=frequency=1500:sample_rate=44100" \
  -filter_complex_script "$WORK/graphs/audio.txt" -map "[aout]" \
  -c:a pcm_s16le -t 7 -y "$WORK/audio_switch.wav" </dev/null &
FFPID=$!
"$PY" "$HERE/s4_control.py" audio --addr tcp://127.0.0.1:5574 \
  --out "$WORK/audio_commands.json"
wait "$FFPID" || warn "ffmpeg exited non-zero"; FFPID=""
"$PY" "$HERE/analyze_audio.py" "$WORK/audio_switch.wav" "$WORK/audio_commands.json" \
  | tee "$WORK/audio_analysis.txt"
fi

########################################################################
if want i; then
log "I  how FFmpeg configures the graph for a matched vs a mismatched branch"
: > "$WORK/negotiation.txt"
for kind in match res fps fmt sar; do
  if [[ $kind == match ]]; then
    mk_graph "$HERE/graphs/switch3.tmpl" "$WORK/graphs/neg_$kind.txt" 5599 320 180
  else
    sed -e "s/__PORT__/5599/g" "$HERE/graphs/mismatch_$kind.tmpl" \
      > "$WORK/graphs/neg_$kind.txt"
  fi
  {
    printf '\n--- %s ---\n' "$kind"
    ffmpeg -nostdin -hide_banner -loglevel verbose -nostats \
      -f lavfi -i "$ASRC" -filter_complex_script "$WORK/graphs/neg_$kind.txt" \
      -map "[v]" -c:v wrapped_avframe -t 0.4 -f null - </dev/null 2>&1 \
      | grep -E 'auto_|streamselect|Parsed_|scale|w:|SAR' | head -25
  } | tee -a "$WORK/negotiation.txt"
done
fi

########################################################################
if want j; then
log "J  luma statistics before and after switching to a mismatched branch"
: > "$WORK/luma.txt"
for kind in res fps fmt sar; do
  [[ -s "$WORK/mismatch_$kind.mkv" ]] || continue
  {
    printf '\n--- %s (frame 45 is pre-switch, frame 120 is post-switch) ---\n' "$kind"
    ffmpeg -nostdin -hide_banner -loglevel info -i "$WORK/mismatch_$kind.mkv" \
      -vf "select=eq(n\\,45)+eq(n\\,120),signalstats,metadata=print" \
      -fps_mode passthrough -f null - </dev/null 2>&1 \
      | grep -E 'pts_time|YMIN|YMAX|YAVG|UAVG|VAVG'
  } | tee -a "$WORK/luma.txt"
done
fi

ok "S4 phases [$PHASES] complete; artefacts in $WORK"
