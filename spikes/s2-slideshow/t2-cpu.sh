#!/usr/bin/env bash
# T2 — CPU cost matrix: producer fps x resolution x pipe format.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DUR=${DUR:-13}; WIN=${WIN:-9}

find "$IMAGES" -name '*.png' -delete 2>/dev/null || true
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 4 --width 1920 --height 1080 >/dev/null

run_case() { # <w> <h> <pfps> <fmt> <label>
  local w=$1 h=$2 pfps=$3 fmt=$4 label=$5
  local fifo="$WORK/s2spike-t2.fifo" prog="$WORK/s2spike-t2.progress"
  local br=3000k buf=6000k
  [[ $h -ge 1080 ]] && { br=5000k; buf=10000k; }
  find "$WORK" -maxdepth 1 -name 's2spike-t2.fifo' -delete 2>/dev/null || true
  mkfifo "$fifo"

  "$PY" "$HERE/producer.py" --dir "$IMAGES" --width "$w" --height "$h" \
    --fps "$pfps" --hold 2 --fade 1.5 --format "$fmt" --stats-interval 4 \
    >"$fifo" 2>"$LOGS/t2-$label-producer.log" &
  local prod=$!; track $prod

  ffmpeg -nostdin -hide_banner -loglevel error -nostats -progress "$prog" \
    -f image2pipe -framerate "$pfps" -i "$fifo" \
    -f lavfi -i "anullsrc=r=44100:cl=stereo" \
    -filter_complex "[0:v]fps=30:start_time=0,realtime,format=yuv420p[v]" \
    -map "[v]" -map 1:a "${YT_ENC[@]}" -r 30 \
    -b:v $br -minrate $br -maxrate $br -bufsize $buf \
    -c:a aac -b:a 128k -ar 44100 -t "$DUR" -f flv -y "$OUT/t2.flv" \
    </dev/null >"$LOGS/t2-$label-ffmpeg.log" 2>&1 &
  local ff=$!; track $ff

  sleep 3
  printf '%s--- %s  %sx%s  producer=%sfps  %s%s\n' "$C_B" "$label" "$w" "$h" "$pfps" "$fmt" "$C_0"
  if kill -0 $prod 2>/dev/null && kill -0 $ff 2>/dev/null; then
    "$PY" "$HERE/cpu.py" "$WIN" "producer:$prod" "ffmpeg:$ff"
  else
    fail "$label: a process died early"; tail -3 "$LOGS/t2-$label-ffmpeg.log"
  fi
  wait $ff 2>/dev/null || true
  kill $prod 2>/dev/null || true
  wait $prod 2>/dev/null || true
  local sp dr du
  sp=$(progress_field "$prog" speed); dr=$(progress_field "$prog" drop_frames)
  du=$(progress_field "$prog" dup_frames)
  echo "  speed=$sp drop=$dr dup=$du  $(tail -1 "$LOGS/t2-$label-producer.log" 2>/dev/null | sed 's/producer stats //')"
}

log "T2 CPU matrix — ${DUR}s runs, ${WIN}s measurement window, YouTube CBR encoder"
for pfps in 1 5 10 15; do
  run_case 1280 720 "$pfps" jpeg "720p-${pfps}fps-jpeg"
done
for pfps in 1 5 10 15; do
  run_case 1920 1080 "$pfps" jpeg "1080p-${pfps}fps-jpeg"
done
log "PNG comparison"
run_case 1280 720 10 png "720p-10fps-png"
run_case 1920 1080 10 png "1080p-10fps-png"
