#!/usr/bin/env bash
# Q7 — can one composer ffmpeg publish full-res and low-res to two relay paths at once,
# and what does the second output add? MediaMTX does not transcode, so the preview rung has
# to be produced here or not at all.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$RESULTS_DIR/t7-dual-output.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT

relay_up
CH="spike01"
DUR=30
MEASURE=20

common=(-nostdin -hide_banner -loglevel warning
  -re -f lavfi -i "testsrc2=size=1920x1080:rate=30"
  -re -f lavfi -i "sine=frequency=440:sample_rate=48000")
venc=(-c:v libx264 -preset veryfast -tune zerolatency -b:v 6000k -maxrate 6000k -bufsize 12000k
  -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p)
lowenc=(-c:v libx264 -preset veryfast -tune zerolatency -b:v 800k -maxrate 800k -bufsize 1600k
  -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p)
aenc=(-c:a aac -b:a 128k -ar 48000 -ac 2)

log "RUN 1 — single 1080p30 output to $CH"
ffmpeg "${common[@]}" -t "$DUR" \
  -map 0:v -map 1:a "${venc[@]}" "${aenc[@]}" -f flv "$RELAY_RTMP/$CH" \
  </dev/null >"$WORK_DIR/t7-single.log" 2>&1 &
single=$!; track "$single"
sleep 5
python3 "$SPIKE_DIR/proc-cpu.py" "$single" "$MEASURE" "single-output composer"
wait "$single" 2>/dev/null || true
sleep 2

log "RUN 2 — 1080p30 program + 854x480 preview from one ffmpeg, two relay paths"
ffmpeg "${common[@]}" -t "$DUR" \
  -filter_complex "[0:v]split=2[full][small];[small]scale=854:480:flags=fast_bilinear[low]" \
  -map "[full]" -map 1:a "${venc[@]}" "${aenc[@]}" -f flv "$RELAY_RTMP/$CH" \
  -map "[low]"  -map 1:a "${lowenc[@]}" -c:a aac -b:a 96k -ar 48000 -ac 2 -f flv "$RELAY_RTMP/$CH/preview" \
  </dev/null >"$WORK_DIR/t7-dual.log" 2>&1 &
dual=$!; track "$dual"
sleep 5
echo "--- relay paths while dual-publishing ---"
http_get "$RELAY_API/v3/paths/list" | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    print(f"  {p[\"name\"]:20} ready={p[\"ready\"]} tracks={p.get(\"tracks\")}")
'
python3 "$SPIKE_DIR/proc-cpu.py" "$dual" "$MEASURE" "dual-output composer"
echo "--- ffprobe both relay outputs ---"
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height -of default=nw=1 "$RELAY_RTMP/$CH" 2>&1 | sed 's/^/  program: /'
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height -of default=nw=1 "$RELAY_RTMP/$CH/preview" 2>&1 | sed 's/^/  preview: /'
wait "$dual" 2>/dev/null || true

echo "--- ffmpeg logs (must be free of 'Past duration' floods / dropped frames) ---"
tail -5 "$WORK_DIR/t7-single.log" | sed 's/^/  single: /'
tail -5 "$WORK_DIR/t7-dual.log"   | sed 's/^/  dual:   /'
