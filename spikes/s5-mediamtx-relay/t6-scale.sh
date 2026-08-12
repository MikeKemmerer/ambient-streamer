#!/usr/bin/env bash
# Q6 — what does the relay itself cost, and does it scale with channel count?
# Publishers republish a pre-encoded bed with -c copy, so almost all measured CPU inside the
# relay container is relay work, not encoding.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$RESULTS_DIR/t6-scale.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT

relay_up
ensure_beds

sample() { # sample <label>
  local label="$1" i cpu mem
  for i in 1 2 3; do
    docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}} {{.NetIO}}' "${PROJECT}-relay-1"
    sleep 2
  done | awk -v l="$label" '{c+=substr($1,1,length($1)-1); n++; mem=$2; net=$5" "$6" "$7}
    END {printf "%-14s relay_cpu_avg=%6.1f%%  mem=%-12s net=%s\n", l, c/n, mem, net}'
  printf '%-14s host_load=%s  ffmpeg_procs=%s\n' "$label" \
    "$(cut -d' ' -f1-3 /proc/loadavg)" "$(pgrep -c -f 'ffmpeg.*ambient-spike-tag' || echo 0)"
}

echo "=== relay resource cost vs channel count ==="
echo "each channel = 1 program path (1080p30 6Mbps) + 1 preview path (480p 800k) + 1 RTMP reader on the program path"
sleep 5
sample "0ch-idle"

started=0
for target in 1 2 4 8; do
  while [[ $started -lt $target ]]; do
    started=$((started+1))
    ch="$(printf 'spike%02d' "$started")"
    p="$(publish_bed "$WORK_DIR/bed.mp4"     "$RELAY_RTMP/$ch"         "$WORK_DIR/t6-$ch.log")"; track "$p"
    p="$(publish_bed "$WORK_DIR/bed-low.mp4" "$RELAY_RTMP/$ch/preview" "$WORK_DIR/t6-$ch-prev.log")"; track "$p"
    sleep 1
    p="$(read_stream "$RELAY_RTMP/$ch" "$WORK_DIR/t6-$ch-reader.log")"; track "$p"
  done
  log "settling at ${target} channels ($((target*2)) paths, ${target} readers)"
  sleep 15
  sample "${target}ch"
done

echo
echo "=== path inventory at 8 channels ==="
http_get "$RELAY_API/v3/paths/list" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("paths:", d["itemCount"])
for p in d["items"]:
    print(f"  {p[\"name\"]:24} ready={p[\"ready\"]} readers={len(p[\"readers\"])} bytesRecv={p[\"bytesReceived\"]/1e6:.1f}MB")
'
echo "=== HLS still servable at 8 channels ==="
for ch in spike01 spike08; do
  echo "  $ch        -> HTTP $(http_code "$RELAY_HLS/$ch/index.m3u8")"
  echo "  $ch/preview-> HTTP $(http_code "$RELAY_HLS/$ch/preview/index.m3u8")"
done
echo "=== relay container detail ==="
docker stats --no-stream "${PROJECT}-relay-1"
pass_fail INFO Q6 "see $OUT for the cost table"
