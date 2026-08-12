#!/usr/bin/env bash
# T1 — does image2pipe slideshow work at all? Record and inspect frames.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

W=${W:-1280}; H=${H:-720}; PFPS=${PFPS:-5}; DUR=${DUR:-14}
REC="$OUT/t1.mp4"; FIFO="$WORK/s2spike-t1.fifo"

log "T1 correctness  ${W}x${H} producer=${PFPS}fps -> 30fps out, ${DUR}s"
require_ffmpeg_filters fps realtime scale format

find "$IMAGES" -name '*.png' -delete 2>/dev/null || true
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 4 --width 1920 --height 1080 >/dev/null

find "$WORK" -maxdepth 1 -name 's2spike-t1.fifo' -delete 2>/dev/null || true
mkfifo "$FIFO"

"$PY" "$HERE/producer.py" --dir "$IMAGES" --width "$W" --height "$H" \
  --fps "$PFPS" --hold 2 --fade 1 --format jpeg \
  >"$FIFO" 2>"$LOGS/t1-producer.log" &
PROD=$!; track $PROD

ffmpeg -nostdin -hide_banner -loglevel warning -nostats \
  -progress "$WORK/s2spike-t1.progress" \
  -f image2pipe -framerate "$PFPS" -i "$FIFO" \
  -filter_complex "[0:v]fps=30:start_time=0,realtime,format=yuv420p[v]" \
  -map "[v]" -c:v libx264 -preset veryfast -crf 20 -t "$DUR" -y "$REC" \
  </dev/null >"$LOGS/t1-ffmpeg.log" 2>&1 &
FF=$!; track $FF

wait $FF; RC=$?
kill $PROD 2>/dev/null || true

[[ -s "$REC" ]] || die "no output file (ffmpeg rc=$RC)"
log "ffmpeg rc=$RC  w/h/nb_frames/rate: $(ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,nb_frames,avg_frame_rate -of csv=p=0 "$REC")"
log "speed=$(progress_field "$WORK/s2spike-t1.progress" speed) frames=$(progress_field "$WORK/s2spike-t1.progress" frame) drop=$(progress_field "$WORK/s2spike-t1.progress" drop_frames) dup=$(progress_field "$WORK/s2spike-t1.progress" dup_frames)"

"$PY" "$HERE/analyze.py" "$REC" --fps 30
tail -2 "$LOGS/t1-producer.log" || true
grep -iE 'error|invalid' "$LOGS/t1-ffmpeg.log" | head -5 || true
