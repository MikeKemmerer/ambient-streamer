#!/usr/bin/env bash
# Option C: does a long-lived Icecast relay hide the Liquidsoap restart from
# FFmpeg entirely? Liquidsoap is a source client here; the composer's HTTP
# connection is to Icecast, which never restarts.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

PRE=20; DOWN="${DOWN:-8}"; POST=30
ICE_PORT="${SPIKE_PORT:-18095}"
ICE_MOUNT="/live"
ICE_IMAGE="${ICE_IMAGE:-libretime/icecast:2.4.4}"
NET="s1-icenet"
WEB="$SPIKE_DIR/icecast/web"
CFG="$SPIKE_DIR/icecast/icecast.xml"
URL="http://127.0.0.1:${ICE_PORT}${ICE_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
RAW="$OUT_DIR/icecast.raw"; FLOG="$OUT_DIR/icecast.ffmpeg.log"
PROG="$OUT_DIR/icecast.progress"; EV="$OUT_DIR/icecast.events"

cleanup() {
  kill_pid "${FF_PID:-}"
  rm_ctr liq-ice icecast
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$WEB"; : > "$EV"; : > "$PROG"

# Generated per run and never committed.
SOURCE_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
sed "s|@SOURCE_PASSWORD@|$SOURCE_PASSWORD|g" "$SPIKE_DIR/icecast/icecast.xml.tmpl" > "$CFG"
chmod 644 "$CFG"

# 100/150 Hz so the fallback is unmistakable against the 330-990 Hz tracks.
if [[ ! -f "$WEB/fallback.mp3" ]]; then
  ff -loglevel error \
    -f lavfi -i "sine=frequency=100:sample_rate=44100:duration=20" \
    -f lavfi -i "sine=frequency=150:sample_rate=44100:duration=20" \
    -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo,volume=-6dB[a]" \
    -map "[a]" -c:a libmp3lame -b:a 256k -ar 44100 -y "$WEB/fallback.mp3"
fi
chmod 644 "$WEB/fallback.mp3"

rm_ctr liq-ice icecast
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null

log "starting icecast on 127.0.0.1:${ICE_PORT}"
docker run -d --name "${CONTAINER_PREFIX}icecast" --network "$NET" \
  -v "$CFG:/etc/icecast.xml:ro" \
  -v "$WEB/fallback.mp3:/usr/share/icecast/web/fallback.mp3:ro" \
  -p "127.0.0.1:${ICE_PORT}:8000" "$ICE_IMAGE" >/dev/null

for _ in $(seq 1 40); do harbor_ready "$ICE_PORT" "live" && break; sleep 0.5; done
harbor_ready "$ICE_PORT" "live" \
  || { docker logs "${CONTAINER_PREFIX}icecast" 2>&1 | tail -20; die "icecast /live never answered 200"; }
ok "icecast serving /live (fallback file active, no source yet)"

log "starting ffmpeg against icecast BEFORE the source connects"
ff -loglevel level+info -progress "$PROG" -stats_period 1 \
   -use_wallclock_as_timestamps 1 \
   -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 \
   -reconnect_on_network_error 1 -reconnect_delay_max 120 \
   -i "$URL" -af "aresample=44100:async=1000:first_pts=0" \
   -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
FF_PID=$!
sleep 5

log "connecting liquidsoap as an icecast source client"
docker run -d --name "${CONTAINER_PREFIX}liq-ice" --network "$NET" \
  -v "$SPIKE_DIR:/spike" -v "$MEDIA_DIR:/media" \
  -e MUSIC_DIR=/media -e ICE_HOST="${CONTAINER_PREFIX}icecast" -e ICE_PORT=8000 \
  -e ICE_MOUNT="$ICE_MOUNT" -e ICE_PASSWORD="$SOURCE_PASSWORD" \
  "$LIQ_IMAGE" liquidsoap /spike/liq/icecast.liq >/dev/null
echo "source_connect $(now)" >> "$EV"
sleep "$PRE"
alive "$FF_PID" || { tail -25 "$FLOG"; die "ffmpeg died before the restart test"; }
T1="$(progress_out_ms "$PROG")"; ok "before kill: out_time_ms=$T1"

log "SIGKILLing the liquidsoap source"
echo "liq_kill $(now)" >> "$EV"
docker kill "${CONTAINER_PREFIX}liq-ice" >/dev/null 2>&1 || true
sleep "$DOWN"
alive "$FF_PID" && ok "ffmpeg alive ${DOWN}s into the source outage" || warn "ffmpeg EXITED"
docker start "${CONTAINER_PREFIX}liq-ice" >/dev/null
echo "liq_restart $(now)" >> "$EV"
sleep "$POST"

T2="$(progress_out_ms "$PROG")"
if alive "$FF_PID"; then ok "RESULT: ffmpeg ALIVE, out_time_ms ${T1} -> ${T2}"
else warn "RESULT: ffmpeg DEAD, out_time_ms stopped at ${T2}"; fi
kill_pid "$FF_PID"; unset FF_PID

echo "---- ffmpeg reconnect/error lines ----"
grep -aiE "reconnect|Connection|error|Invalid|End of file" "$FLOG" | tail -12 || echo "(none)"
echo "---- icecast log ----"
docker logs "${CONTAINER_PREFIX}icecast" 2>&1 | grep -iE "fallback|source|listener|move" | tail -12 || true
echo "---- audio analysis (100/150 Hz segments are the fallback) ----"
"$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$RAW"
docker logs "${CONTAINER_PREFIX}liq-ice" > "$OUT_DIR/icecast.liq.log" 2>&1 || true
