#!/usr/bin/env bash
# Q2, decisive form: is the outage FILLED with silence (audio timeline keeps
# tracking wallclock, A/V stays aligned) or DROPPED (timeline compresses, audio
# falls permanently behind video)?
#
# The source is a metronome: a 1 kHz/1.5 kHz burst for 0.4 s at the top of every
# second. One pulse == one second of source audio, so the recording carries its
# own clock.
#   filled  -> pulses ~= duration - outage  AND  one long silence run ~= outage
#   dropped -> pulses ~= duration           AND  no long silence run
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PRE=20; DOWN="${DOWN:-8}"; POST=30
HARBOR_PORT="${SPIKE_PORT:-18098}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
PULSE_DIR="$SPIKE_DIR/media-pulse"
RAW="$OUT_DIR/timeline.raw"; FLOG="$OUT_DIR/timeline.ffmpeg.log"
PROG="$OUT_DIR/timeline.progress"

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-timeline; }
trap cleanup EXIT

mkdir -p "$PULSE_DIR"
if [[ ! -f "$PULSE_DIR/pulse.mp3" ]]; then
  log "generating metronome source"
  ff -loglevel error \
    -f lavfi -i "sine=frequency=1000:sample_rate=44100:duration=300" \
    -f lavfi -i "sine=frequency=1500:sample_rate=44100:duration=300" \
    -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo,volume=volume='lt(mod(t\,1)\,0.4)':eval=frame[a]" \
    -map "[a]" -c:a libmp3lame -b:a 256k -ar 44100 -y "$PULSE_DIR/pulse.mp3"
fi
MEDIA_DIR="$PULSE_DIR"

rm_ctr liq-timeline
liq_run liq-timeline /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || die "harbor mount never answered 200"

# -probesize/-analyzeduration kept small so the first write is not delayed by
# five seconds of buffering, which would corrupt the wallclock reference.
ff -loglevel level+warning -progress "$PROG" -stats_period 1 \
   -probesize 32k -analyzeduration 500000 \
   -use_wallclock_as_timestamps 1 \
   -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 \
   -reconnect_on_network_error 1 -reconnect_delay_max 120 \
   -i "$URL" -af "aresample=44100:async=1000:first_pts=0" \
   -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
FF_PID=$!

T_FIRST=""
for _ in $(seq 1 300); do [[ -s "$RAW" ]] && { T_FIRST="$(now)"; break; }; sleep 0.1; done
[[ -n "$T_FIRST" ]] || die "no audio at all"
ok "first audio byte at $(date -d "@$T_FIRST" +%T.%3N)"

sleep "$PRE"
docker kill "${CONTAINER_PREFIX}liq-timeline" >/dev/null 2>&1 || true
T_KILL="$(now)"
sleep "$DOWN"
docker start "${CONTAINER_PREFIX}liq-timeline" >/dev/null
wait_harbor || true
T_READY="$(now)"
sleep "$POST"
T_STOP="$(now)"
alive "$FF_PID" && ok "ffmpeg ALIVE" || warn "ffmpeg DEAD"
kill_pid "$FF_PID"; unset FF_PID

WALL="$(echo "$T_STOP - $T_FIRST" | bc)"
OUTAGE="$(echo "$T_READY - $T_KILL" | bc)"
echo "wallclock_span=${WALL}s  source_outage=${OUTAGE}s"
"$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$RAW" --window 0.05 --silence-db -55 \
  > "$OUT_DIR/timeline.probe.json"
"$PY" - "$OUT_DIR/timeline.probe.json" "$WALL" "$OUTAGE" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
wall, outage = float(sys.argv[2]), float(sys.argv[3])
dur, pulses = d["duration_s"], d["loud_runs"]
longest = d["max_silence_run_s"]
print(f"recorded_duration={dur}s  pulses(=source seconds delivered)={pulses}")
print(f"longest_silence_run={longest}s")
print(f"duration_vs_wallclock={round(dur - wall, 2)}s   pulses_vs_duration={round(pulses - dur, 1)}")
verdict = "FILLED (timeline preserved)" if longest > 2.0 else "DROPPED (timeline compressed)"
print(f"VERDICT: outage was {verdict}")
EOF
grep -aiE "reconnect|Stream ends" "$FLOG" | tail -8 || true
