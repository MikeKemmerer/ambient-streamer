#!/usr/bin/env bash
# Q3 — is the HLS preview a real, readable stream?
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$RESULTS_DIR/t3-hls.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT INT TERM

relay_up
ensure_beds
CH="spike01"

p1="$(publish_bed "$WORK_DIR/bed.mp4"     "$RELAY_RTMP/$CH"         "$WORK_DIR/t3-prog.log")"; track "$p1"
p2="$(publish_bed "$WORK_DIR/bed-low.mp4" "$RELAY_RTMP/$CH/preview" "$WORK_DIR/t3-prev.log")"; track "$p2"

# hlsAlwaysRemux should have the muxer running before anyone asks; time how long until the
# playlist is actually servable.
t0="$(now_ms)"
for i in $(seq 1 60); do
  code="$(http_code "$RELAY_HLS/$CH/preview/index.m3u8")"
  [[ "$code" == "200" ]] && break
  sleep 0.5
done
t1="$(now_ms)"
echo "preview playlist first served after $((t1-t0)) ms (HTTP $code)"

echo "--- master playlist ---"
http_get "$RELAY_HLS/$CH/preview/index.m3u8"
echo "--- media playlist ---"
media="$(http_get "$RELAY_HLS/$CH/preview/index.m3u8" | grep -v '^#' | grep -v '^$' | head -1)"
http_get "$RELAY_HLS/$CH/preview/$media" | tee "$WORK_DIR/t3-media.m3u8"
echo "--- segments referenced: $(grep -c '\.ts' "$WORK_DIR/t3-media.m3u8" || true) ---"

seg="$(grep '\.ts' "$WORK_DIR/t3-media.m3u8" | head -1)"
python3 - "$RELAY_HLS/$CH/preview/$seg" "$WORK_DIR/t3-seg.ts" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=10) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())
PY
echo "first segment bytes: $(stat -c%s "$WORK_DIR/t3-seg.ts")"
echo "--- ffprobe of downloaded segment ---"
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height,r_frame_rate,sample_rate -of default=nw=1 "$WORK_DIR/t3-seg.ts"

echo "--- ffprobe of live preview playlist ---"
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height,r_frame_rate -of default=nw=1 "$RELAY_HLS/$CH/preview/index.m3u8"
echo "--- ffprobe of live program playlist ---"
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height,r_frame_rate -of default=nw=1 "$RELAY_HLS/$CH/index.m3u8"

echo "--- decode 10s of the preview through ffmpeg ---"
set +e
ffmpeg -nostdin -hide_banner -loglevel info -i "$RELAY_HLS/$CH/preview/index.m3u8" \
  -t 10 -f null - </dev/null 2>"$WORK_DIR/t3-decode.log"
dec_rc=$?
set -e
tail -4 "$WORK_DIR/t3-decode.log"
frames="$(grep -oE 'frame= *[0-9]+' "$WORK_DIR/t3-decode.log" | tail -1 | grep -oE '[0-9]+' || echo 0)"
echo "decoded frames=$frames rc=$dec_rc"

if [[ "$code" == "200" && "$dec_rc" -eq 0 && "$frames" -ge 200 ]]; then
  pass_fail PASS Q3 "HLS playlist+segments served; ffmpeg decoded $frames frames of preview in 10s"
else
  pass_fail FAIL Q3 "http=$code decode_rc=$dec_rc frames=$frames"
fi
