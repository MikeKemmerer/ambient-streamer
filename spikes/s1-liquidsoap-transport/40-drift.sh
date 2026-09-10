#!/usr/bin/env bash
# Q4: does audio drift against a continuously running video pipeline?
# Two consumers: the composer (video+audio, production-shaped) and a raw
# uncompensated PCM tap whose sample count against wallclock gives the
# Liquidsoap source clock rate directly.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

DURATION="${DURATION:-1200}"
HARBOR_PORT="${SPIKE_PORT:-18094}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
FLV="$OUT_DIR/drift.flv"; RAW="$OUT_DIR/drift.raw"
CLOG="$OUT_DIR/drift.composer.log"; RLOG="$OUT_DIR/drift.tap.log"
CPROG="$OUT_DIR/drift.composer.progress"; SAMPLES="$OUT_DIR/drift.samples.csv"

cleanup() { kill_pid "${COMP_PID:-}"; kill_pid "${TAP_PID:-}"; rm_ctr liq-drift; }
trap cleanup EXIT

log "starting harbor"
liq_run liq-drift /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || die "harbor mount never answered 200"

: > "$CPROG"; : > "$SAMPLES"

log "starting composer (video paced by fps+realtime, audio from harbor)"
ff -loglevel level+warning -progress "$CPROG" -stats_period 5 \
   -f lavfi -i "color=c=0x101820:s=640x360:r=30" \
   -use_wallclock_as_timestamps 1 \
   -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 \
   -reconnect_on_network_error 1 -reconnect_delay_max 120 -i "$URL" \
   -filter_complex "[0:v]fps=30,realtime[v];[1:a]aresample=44100:async=1000:first_pts=0[a]" \
   -map "[v]" -map "[a]" \
   -c:v libx264 -preset ultrafast -b:v 800k -minrate 800k -maxrate 800k -bufsize 1600k \
   -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p -fps_mode cfr \
   -c:a aac -b:a 128k -ar 44100 -max_interleave_delta 0 \
   -f flv -y "$FLV" >"$CLOG" 2>&1 &
COMP_PID=$!

log "starting uncompensated PCM tap"
ff -loglevel level+warning -i "$URL" -c:a pcm_s16le -ar 44100 -ac 2 \
   -f s16le -y "$RAW" >"$RLOG" 2>&1 &
TAP_PID=$!

log "running for ${DURATION}s; sampling tap size every 30s"
END=$(( $(date +%s) + DURATION ))
while [[ $(date +%s) -lt $END ]]; do
  sleep 30
  printf '%s,%s\n' "$(now)" "$(stat -c %s "$RAW" 2>/dev/null || echo 0)" >> "$SAMPLES"
  alive "$COMP_PID" || { warn "composer died early"; break; }
  alive "$TAP_PID"  || { warn "tap died early"; break; }
done

kill_pid "$COMP_PID"; kill_pid "$TAP_PID"; unset COMP_PID TAP_PID
rm_ctr liq-drift

echo "---- composer speed samples ----"
grep -a '^speed=' "$CPROG" | tail -5
grep -a '^out_time=' "$CPROG" | tail -1
echo "---- composer warnings ----"
grep -aiE "error|drop|dup|non-monotonic|buffer" "$CLOG" | sort | uniq -c | sort -rn | head -10 || echo "(none)"
echo "---- drift ----"
"$PY" "$SPIKE_DIR/analyze/drift.py" --samples "$SAMPLES" --container "$FLV" | tee "$OUT_DIR/drift.json"
