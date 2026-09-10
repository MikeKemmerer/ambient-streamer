#!/usr/bin/env bash
# T4 — add and remove images mid-run; ffmpeg PID must never change.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DUR=${DUR:-26}; W=${W:-1280}; H=${H:-720}; PFPS=${PFPS:-5}
FIFO="$WORK/s2spike-t4.fifo"; PROG="$WORK/s2spike-t4.progress"; REC="$OUT/t4.mp4"

find "$IMAGES" -name '*.png' -delete 2>/dev/null || true
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 2 --width 1280 --height 720 >/dev/null
find "$WORK" -maxdepth 1 -name 's2spike-t4.fifo' -delete 2>/dev/null || true
mkfifo "$FIFO"

log "T4 hot image-set change — ${DUR}s, hold=2s fade=1s"
"$PY" "$HERE/producer.py" --dir "$IMAGES" --width "$W" --height "$H" \
  --fps "$PFPS" --hold 2 --fade 1 --stats-interval 4 \
  >"$FIFO" 2>"$LOGS/t4-producer.log" &
PROD=$!; track $PROD
ffmpeg -nostdin -hide_banner -loglevel error -nostats -progress "$PROG" \
  -f image2pipe -framerate "$PFPS" -i "$FIFO" \
  -filter_complex "[0:v]fps=30:start_time=0,realtime,format=yuv420p[v]" \
  -map "[v]" -c:v libx264 -preset veryfast -crf 22 -t "$DUR" -y "$REC" \
  </dev/null >"$LOGS/t4-ffmpeg.log" 2>&1 &
FF=$!; track $FF
log "ffmpeg pid at start: $FF   producer pid: $PROD"

sleep 8
log "t=8s  ADD img04..img05"
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 2 --start 4 \
  --width 1280 --height 720 >/dev/null
kill -0 $FF && log "  ffmpeg still $FF (alive)"

sleep 8
log "t=16s  REMOVE img00"
find "$IMAGES" -name 'img00.png' -delete
kill -0 $FF && log "  ffmpeg still $FF (alive)"

wait $FF; RC=$?
kill $PROD 2>/dev/null || true
FF_END=$(pgrep -f 's2spike-t4' || echo "")
log "ffmpeg rc=$RC  pid_at_start=$FF  survivors='${FF_END}'"
log "speed=$(progress_field "$PROG" speed) frames=$(progress_field "$PROG" frame) drop=$(progress_field "$PROG" drop_frames) dup=$(progress_field "$PROG" dup_frames)"
"$PY" "$HERE/analyze.py" "$REC" --fps 30
grep -c 'producer stats' "$LOGS/t4-producer.log" >/dev/null && \
  grep 'producer stats' "$LOGS/t4-producer.log" | sed 's/^/  /'
