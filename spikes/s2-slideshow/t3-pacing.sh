#!/usr/bin/env bash
# T3 — does fps=+realtime hold speed=1.0x with drop=0 dup=0 at the FLV muxer?
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DUR=${DUR:-20}; W=${W:-1280}; H=${H:-720}; PFPS=${PFPS:-5}

find "$IMAGES" -name '*.png' -delete 2>/dev/null || true
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 4 --width 1920 --height 1080 >/dev/null

variant() { # <label> <filter>
  local label=$1 vf=$2
  local fifo="$WORK/s2spike-t3.fifo" prog="$WORK/s2spike-t3-$label.progress"
  find "$WORK" -maxdepth 1 -name 's2spike-t3.fifo' -delete 2>/dev/null || true
  mkfifo "$fifo"
  "$PY" "$HERE/producer.py" --dir "$IMAGES" --width "$W" --height "$H" \
    --fps "$PFPS" --hold 2 --fade 1.5 --stats-interval 100 \
    >"$fifo" 2>"$LOGS/t3-$label-producer.log" &
  local prod=$!; track $prod
  ffmpeg -nostdin -hide_banner -loglevel error -nostats -progress "$prog" \
    -f image2pipe -framerate "$PFPS" -i "$fifo" \
    -f lavfi -i "anullsrc=r=44100:cl=stereo" \
    -filter_complex "[0:v]$vf[v]" \
    -map "[v]" -map 1:a "${YT_ENC[@]}" -r 30 \
    -b:v 3000k -minrate 3000k -maxrate 3000k -bufsize 6000k \
    -c:a aac -b:a 128k -ar 44100 -t "$DUR" -f flv -y "$OUT/t3-$label.flv" \
    </dev/null >"$LOGS/t3-$label-ffmpeg.log" 2>&1 &
  local ff=$!; track $ff
  wait $ff || true
  kill $prod 2>/dev/null || true; wait $prod 2>/dev/null || true

  local sp fr dr du tt
  sp=$(progress_field "$prog" speed); fr=$(progress_field "$prog" frame)
  dr=$(progress_field "$prog" drop_frames); du=$(progress_field "$prog" dup_frames)
  tt=$(progress_field "$prog" out_time)
  printf '  %-22s speed=%-7s frames=%-5s drop=%-4s dup=%-4s out_time=%s\n' \
    "$label" "$sp" "$fr" "$dr" "$du" "$tt"
  # Inter-frame arrival jitter at the muxer: bursts show up as a wide spread.
  "$PY" "$HERE/jitter.py" "$prog"
}

log "T3 pacing — producer ${PFPS}fps -> 30fps FLV, ${DUR}s each"
variant "with-realtime" "fps=30:start_time=0,realtime,format=yuv420p"
variant "no-realtime"   "fps=30:start_time=0,format=yuv420p"
