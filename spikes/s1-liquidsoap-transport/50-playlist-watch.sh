#!/usr/bin/env bash
# Q5: does playlist(reload_mode="watch") pick up a file dropped into the media
# directory mid-run, with no Liquidsoap restart and no break in the stream?
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

# Private media directory: this test mutates it, and other spikes read the shared one.
SRC_MEDIA="$MEDIA_DIR"
MEDIA_DIR="$SPIKE_DIR/media-watch"
mkdir -p "$MEDIA_DIR"
cp -f "$SRC_MEDIA"/*.mp3 "$MEDIA_DIR/"

RUNTIME="${RUNTIME:-230}"; ADD_AT="${ADD_AT:-20}"
NEW_SRC="$SPIKE_DIR/hold/t9-new.mp3"; NEW_DST="$MEDIA_DIR/t9-new.mp3"
HARBOR_PORT="${SPIKE_PORT:-18093}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
RAW="$OUT_DIR/watch.raw"; FLOG="$OUT_DIR/watch.ffmpeg.log"

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-watch; rm -f "$NEW_DST" 2>/dev/null || true; }
trap cleanup EXIT

[[ -f "$NEW_SRC" ]] || die "missing $NEW_SRC — run 00-setup.sh"
rm -f "$NEW_DST" 2>/dev/null || true

log "starting harbor with the 4-track directory"
liq_run liq-watch /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || die "harbor mount never answered 200"

ff -loglevel level+warning -use_wallclock_as_timestamps 1 \
   -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 \
   -reconnect_on_network_error 1 -reconnect_delay_max 120 -i "$URL" \
   -af "aresample=44100:async=1000:first_pts=0" \
   -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
FF_PID=$!

sleep "$ADD_AT"
LIQ_PID_BEFORE="$(docker inspect -f '{{.State.Pid}}' "${CONTAINER_PREFIX}liq-watch")"
log "adding t9-new.mp3 (1200/1800 Hz) to the media directory"
cp "$NEW_SRC" "$NEW_DST"
ADDED="$(now)"

sleep "$(( RUNTIME - ADD_AT ))"

LIQ_PID_AFTER="$(docker inspect -f '{{.State.Pid}}' "${CONTAINER_PREFIX}liq-watch")"
alive "$FF_PID" && ok "ffmpeg still alive" || warn "ffmpeg died"
kill_pid "$FF_PID"; unset FF_PID
docker logs "${CONTAINER_PREFIX}liq-watch" > "$OUT_DIR/watch.liq.log" 2>&1 || true

echo "---- liquidsoap pid: before=$LIQ_PID_BEFORE after=$LIQ_PID_AFTER ----"
[[ "$LIQ_PID_BEFORE" == "$LIQ_PID_AFTER" ]] && ok "same process, no restart" || warn "liquidsoap RESTARTED"
echo "---- reload evidence ----"
grep -aiE "reload|watch|Prepared|SPIKE_TRACK" "$OUT_DIR/watch.liq.log" | tail -25 || echo "(none)"
echo "---- did the new file play? ----"
grep -a "t9-new" "$OUT_DIR/watch.liq.log" && ok "liquidsoap played t9-new.mp3" || warn "t9-new.mp3 never played"
echo "---- audio analysis (expect a 1200/1800 Hz segment, zero silence runs) ----"
"$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$RAW"
