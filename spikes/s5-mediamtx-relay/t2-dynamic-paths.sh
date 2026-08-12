#!/usr/bin/env bash
# Q2 — can a composer publish into a regex-matched dynamic path (any channel name)?
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$RESULTS_DIR/t2-dynamic-paths.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT INT TERM

relay_up
ensure_beds

# Channel names invented at runtime — nothing about them exists in the config.
CH1="spike01"
CH2="zz-ambient-lofi-9"

log "publishing program + preview for two never-configured channel names"
p1="$(publish_bed "$WORK_DIR/bed.mp4"     "$RELAY_RTMP/$CH1"         "$WORK_DIR/t2-$CH1.log")"; track "$p1"
p2="$(publish_bed "$WORK_DIR/bed-low.mp4" "$RELAY_RTMP/$CH1/preview" "$WORK_DIR/t2-$CH1-prev.log")"; track "$p2"
p3="$(publish_bed "$WORK_DIR/bed.mp4"     "$RELAY_RTMP/$CH2"         "$WORK_DIR/t2-$CH2.log")"; track "$p3"
p4="$(publish_bed "$WORK_DIR/bed-low.mp4" "$RELAY_RTMP/$CH2/preview" "$WORK_DIR/t2-$CH2-prev.log")"; track "$p4"
sleep 6

echo "--- API path list ---"
http_get "$RELAY_API/v3/paths/list" | python3 -m json.tool | grep -E '"name"|"ready"|"bytesReceived"|"state"'
echo "--- end ---"

accepted=0
for p in "$CH1" "$CH1/preview" "$CH2" "$CH2/preview"; do
  ready="$(http_get "$RELAY_API/v3/paths/get/${p//\//%2F}" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["ready"])' 2>/dev/null || echo "missing")"
  echo "path $p ready=$ready"
  [[ "$ready" == "True" ]] && accepted=$((accepted+1))
done

# Negative controls: the regex must actually refuse anything that is not a channel name.
rejected=0
for bad in "Bad_Name" "a/b/c" "../etc"; do
  set +e
  timeout 8 ffmpeg -nostdin -hide_banner -loglevel error -re -t 3 \
    -i "$WORK_DIR/bed-low.mp4" -c copy -f flv "$RELAY_RTMP/$bad" </dev/null >"$WORK_DIR/t2-bad.log" 2>&1
  rc=$?
  set -e
  echo "reject-test '$bad' -> ffmpeg rc=$rc : $(tail -1 "$WORK_DIR/t2-bad.log")"
  [[ $rc -ne 0 ]] && rejected=$((rejected+1))
done

echo "--- relay log (tail) ---"
docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" logs --no-color relay 2>&1 | tail -25

if [[ $accepted -eq 4 && $rejected -eq 3 ]]; then
  pass_fail PASS Q2 "4/4 dynamic paths accepted for 2 unconfigured channel names; 3/3 invalid names refused"
else
  pass_fail FAIL Q2 "accepted=$accepted/4 rejected=$rejected/3"
fi
