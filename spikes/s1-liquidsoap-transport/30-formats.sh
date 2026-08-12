#!/usr/bin/env bash
# Q3: which container/codec should Liquidsoap emit? Compare restart resync,
# CPU, and connect latency for mp3 / ogg-vorbis / ogg-flac / wav.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

PRE=15; DOWN=5; POST=25
HARBOR_PORT="${SPIKE_PORT:-18091}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
RECONNECT=(-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
           -reconnect_on_network_error 1 -reconnect_delay_max 120)
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
SUMMARY="$OUT_DIR/formats.summary.txt"; : > "$SUMMARY"

declare -A ENC=(
  [mp3]='enc = %mp3(bitrate=256, samplerate=44100, stereo=true)'
  [vorbis]='enc = %vorbis(quality=0.6, samplerate=44100, channels=2)'
  [oggflac]='enc = %ogg(%flac(compression=5, samplerate=44100, channels=2, bits_per_sample=16))'
  [wav]='enc = %wav(stereo=true, samplerate=44100)'
)

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-mp3 liq-vorbis liq-oggflac liq-wav; }
trap cleanup EXIT

mkdir -p "$SPIKE_DIR/liq/gen"

for FMT in $FORMATS; do
  log "=== format=$FMT icy=$ICY ==="
  GEN="$SPIKE_DIR/liq/gen/harbor-$FMT.liq"
  sed "s|^enc = .*# ENCODER_LINE|${ENC[$FMT]}  # ENCODER_LINE|" "$SPIKE_DIR/liq/harbor.liq" > "$GEN"
  grep -q '^enc = ' "$GEN" || die "encoder substitution failed for $FMT"

  docker rm -f "${CONTAINER_PREFIX}liq-$FMT" >/dev/null 2>&1 || true
  liq_run "liq-$FMT" "/spike/liq/gen/harbor-$FMT.liq" -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
  UP=0; wait_harbor && UP=1
  if [[ $UP -eq 0 ]]; then
    warn "$FMT: harbor mount never answered 200"
    docker logs "${CONTAINER_PREFIX}liq-$FMT" 2>&1 | tail -15
    echo "$FMT: LIQUIDSOAP FAILED TO START" >> "$SUMMARY"
    docker rm -f "${CONTAINER_PREFIX}liq-$FMT" >/dev/null; continue
  fi

  RAW="$OUT_DIR/fmt-$FMT-icy$ICY.raw"; FLOG="$OUT_DIR/fmt-$FMT-icy$ICY.ffmpeg.log"
  PROG="$OUT_DIR/fmt-$FMT-icy$ICY.progress"
  : > "$PROG"
  T_START="$(now)"
  ff -loglevel level+info -progress "$PROG" -stats_period 1 \
     -icy "$ICY" -use_wallclock_as_timestamps 1 "${RECONNECT[@]}" \
     -i "$URL" -af "aresample=44100:async=1000:first_pts=0" \
     -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
  FF_PID=$!
  TTFB="n/a"
  for _ in $(seq 1 200); do
    [[ -s "$RAW" ]] && { TTFB="$(echo "$(now) - $T_START" | bc)"; break; }
    alive "$FF_PID" || break
    sleep 0.1
  done

  # CPU while steady: liquidsoap from docker stats, ffmpeg from /proc.
  sleep "$PRE"
  LCPU="$(docker stats --no-stream --format '{{.CPUPerc}}' "${CONTAINER_PREFIX}liq-$FMT" 2>/dev/null || echo n/a)"
  FCPU="$(ps -o %cpu= -p "$FF_PID" 2>/dev/null | tr -d ' ' || echo n/a)"

  alive "$FF_PID" || { warn "$FMT: ffmpeg died before the restart test"; }
  docker kill "${CONTAINER_PREFIX}liq-$FMT" >/dev/null 2>&1 || true
  sleep "$DOWN"
  docker start "${CONTAINER_PREFIX}liq-$FMT" >/dev/null
  sleep "$POST"

  if alive "$FF_PID"; then SURV="ALIVE"; else SURV="DEAD"; fi
  kill_pid "$FF_PID"; unset FF_PID

  ERRS="$(grep -acEi "invalid|error|corrupt|non-monotonic|missing|Could not find" "$FLOG" || true)"
  echo "--- $FMT (icy=$ICY): survived=$SURV ttfb=${TTFB}s liq_cpu=$LCPU ffmpeg_cpu=${FCPU}% ffmpeg_err_lines=$ERRS" | tee -a "$SUMMARY"
  "$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$RAW" > "$OUT_DIR/fmt-$FMT-icy$ICY.probe.json" 2>&1 || true
  "$PY" - "$OUT_DIR/fmt-$FMT-icy$ICY.probe.json" <<'EOF' | tee -a "$SUMMARY"
import json,sys
d=json.load(open(sys.argv[1]))
runs=d.get("silence_runs",[])
tones=[t["hz"] for t in d.get("tone_segments",[]) if t["duration_s"]>=1.0]
print(f"    dur={d.get('duration_s')}s silence_pct={d.get('silence_pct')} "
      f"gaps={[r['duration_s'] for r in runs]} tone_pairs={tones[:8]}")
EOF
  grep -aiE "error|invalid|corrupt|Could not find" "$FLOG" | sort | uniq -c | sort -rn | head -5 >> "$SUMMARY" || true
  docker logs "${CONTAINER_PREFIX}liq-$FMT" > "$OUT_DIR/fmt-$FMT-icy$ICY.liq.log" 2>&1 || true
  docker rm -f "${CONTAINER_PREFIX}liq-$FMT" >/dev/null
done

echo; ok "summary written to $SUMMARY"; cat "$SUMMARY"
