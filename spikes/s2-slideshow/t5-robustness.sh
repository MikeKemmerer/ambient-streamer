#!/usr/bin/env bash
# T5 — backpressure and producer death. What must the watchdog detect?
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

W=${W:-1280}; H=${H:-720}; PFPS=${PFPS:-5}
FIFO="$WORK/s2spike-t5.fifo"; PROG="$WORK/s2spike-t5.progress"

find "$IMAGES" -name '*.png' -delete 2>/dev/null || true
"$PY" "$HERE/make_images.py" --dir "$IMAGES" --count 3 --width 1280 --height 720 >/dev/null

scenario() { # <label> <action> ; action runs at t=8s
  local label=$1 action=$2
  find "$WORK" -maxdepth 1 -name 's2spike-t5.fifo' -delete 2>/dev/null || true
  mkfifo "$FIFO"
  : >"$PROG"
  "$PY" "$HERE/producer.py" --dir "$IMAGES" --width "$W" --height "$H" \
    --fps "$PFPS" --hold 2 --fade 1 --stats-interval 100 \
    >"$FIFO" 2>"$LOGS/t5-$label-producer.log" &
  local prod=$!; track $prod
  ffmpeg -nostdin -hide_banner -loglevel warning -nostats -progress "$PROG" \
    -f image2pipe -framerate "$PFPS" -i "$FIFO" \
    -filter_complex "[0:v]fps=30:start_time=0,realtime,format=yuv420p[v]" \
    -map "[v]" -c:v libx264 -preset veryfast -crf 24 -t 18 -f mp4 -y "$OUT/t5-$label.mp4" \
    </dev/null >"$LOGS/t5-$label-ffmpeg.log" 2>&1 &
  local ff=$!; track $ff

  sleep 8
  local t_before; t_before=$(progress_field "$PROG" out_time_us)
  printf '%s--- %s%s  at t=8s: %s   (out_time=%sus)\n' "$C_B" "$label" "$C_0" "$action" "$t_before"
  case "$action" in
    stop)  kill -STOP $prod ;;
    kill)  kill -9 $prod ;;
    slow)  kill -STOP $prod; sleep 2; kill -CONT $prod ;;
  esac

  sleep 6
  local alive="dead"; kill -0 $ff 2>/dev/null && alive="ALIVE"
  local t_after; t_after=$(progress_field "$PROG" out_time_us)
  local adv=$(( (${t_after:-0} - ${t_before:-0}) / 1000 ))
  echo "  after 6s: ffmpeg=$alive  media advanced ${adv}ms in 6000ms wall"
  [[ "$action" == "stop" ]] && kill -CONT $prod 2>/dev/null
  kill -TERM $ff 2>/dev/null; wait $ff 2>/dev/null || true
  kill -9 $prod 2>/dev/null; wait $prod 2>/dev/null || true
  echo "  ffmpeg log tail: $(tail -2 "$LOGS/t5-$label-ffmpeg.log" | tr '\n' ' ')"
}

log "T5 backpressure and producer failure"
scenario "producer-stalls" stop
scenario "producer-dies"   kill
scenario "producer-slow"   slow
