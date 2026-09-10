#!/usr/bin/env bash
# The question that actually decides the transport: when the audio input stalls
# for the reconnect window, does the composer stop emitting VIDEO as well?
# A 24/7 stream can tolerate silence; it cannot tolerate the publisher going
# quiet, because that is what ends a YouTube broadcast.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

PRE=20; DOWN="${DOWN:-8}"; POST=35
HARBOR_PORT="${SPIKE_PORT:-18099}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
FLV="$OUT_DIR/composer-restart.flv"; CLOG="$OUT_DIR/composer-restart.log"
PROG="$OUT_DIR/composer-restart.progress"; BYTES="$OUT_DIR/composer-restart.bytes.csv"

cleanup() { kill_pid "${FF_PID:-}"; kill "${SAMPLER_PID:-0}" 2>/dev/null || true; rm_ctr liq-comp; }
trap cleanup EXIT

rm_ctr liq-comp
liq_run liq-comp /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || die "harbor mount never answered 200"
: > "$PROG"

log "starting full composer (lavfi video + harbor audio -> flv)"
ff -loglevel level+warning -progress "$PROG" -stats_period 1 \
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
FF_PID=$!

# Bytes written to the muxer is the quantity YouTube actually sees. Sample it
# continuously so the outage can be reported as "seconds with no data on the wire".
: > "$BYTES"
( while kill -0 "$FF_PID" 2>/dev/null; do
    printf '%s,%s\n' "$(now)" "$(stat -c %s "$FLV" 2>/dev/null || echo 0)" >> "$BYTES"
    sleep 0.5
  done ) &
SAMPLER_PID=$!

sleep "$PRE"
alive "$FF_PID" || { tail -20 "$CLOG"; die "composer died early"; }
docker kill "${CONTAINER_PREFIX}liq-comp" >/dev/null 2>&1 || true
T_KILL="$(now)"
sleep "$DOWN"
docker start "${CONTAINER_PREFIX}liq-comp" >/dev/null
wait_harbor || true
T_READY="$(now)"
sleep "$POST"
alive "$FF_PID" && ok "composer ALIVE" || warn "composer DEAD"
kill_pid "$FF_PID"; unset FF_PID
kill "$SAMPLER_PID" 2>/dev/null || true
rm_ctr liq-comp

echo "source_outage=$(echo "$T_READY - $T_KILL" | bc)s"
echo "---- longest interval with ZERO bytes written to the output ----"
awk -F, 'NF==2{t=$1+0; b=$2+0;
  if(prev_b!="" && b==prev_b){ if(stall_start=="") stall_start=prev_t }
  else if(prev_b!=""){ if(stall_start!=""){ d=prev_t-stall_start; if(d>m){m=d; at=stall_start} } stall_start="" }
  prev_b=b; prev_t=t }
  END{ if(stall_start!=""){ d=prev_t-stall_start; if(d>m){m=d; at=stall_start} }
       printf "max_no_data_interval=%.2fs\n", m }' "$BYTES"
echo "---- progress blocks (one per second of wallclock while producing) ----"
echo "blocks=$(grep -ac '^progress=' "$PROG")  final_out_time=$(grep -a '^out_time=' "$PROG" | tail -1)"
echo "---- largest gap between consecutive VIDEO packet pts ----"
ffprobe -v error -select_streams v -show_entries packet=pts_time -of csv=p=0 "$FLV" \
  | awk -F, 'NF{t=$1+0; if(n++){d=t-p; if(d>m){m=d; at=p}} p=t} END{
      printf "video_packets=%d  max_pts_gap=%.3fs at t=%.2fs  last_pts=%.2fs\n", n, m, at, p}'
echo "---- largest gap between consecutive AUDIO packet pts ----"
ffprobe -v error -select_streams a -show_entries packet=pts_time -of csv=p=0 "$FLV" \
  | awk -F, 'NF{t=$1+0; if(n++){d=t-p; if(d>m){m=d; at=p}} p=t} END{
      printf "audio_packets=%d  max_pts_gap=%.3fs at t=%.2fs  last_pts=%.2fs\n", n, m, at, p}'
echo "---- composer warnings ----"
grep -aiE "error|reconnect|Stream ends|drop|dup" "$CLOG" | sort | uniq -c | sort -rn | head -8 || echo "(none)"
