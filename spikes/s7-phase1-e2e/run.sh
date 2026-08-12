#!/usr/bin/env bash
# Phase 1 end-to-end: liquidsoap -> icecast -> composer -> mediamtx (local relay).
# Nothing here talks to YouTube.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$HERE/work"
OUT="$HERE/out"
NET="p1-net"
PREFIX="p1-"
RUNTIME="${RUNTIME:-60}"

ICE_PORT=8081
RTMP_PORT=1935
HLS_PORT=8888
CHANNEL=lofi
WIDTH=1280; HEIGHT=720; FPS=30; PRODUCER_FPS=10

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s %s\n' "$BLU" "$NC" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s[!!]%s %s\n' "$YLW" "$NC" "$*"; }
die()  { printf '%s[XX]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

ff() { ffmpeg -nostdin -hide_banner "$@" </dev/null; }

COMPOSER_PID=""
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$COMPOSER_PID" ]] && kill -0 "$COMPOSER_PID" 2>/dev/null; then
    kill -INT "$COMPOSER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$COMPOSER_PID" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$COMPOSER_PID" 2>/dev/null || true
  fi
  pkill -INT -f 'ambient-e2e-composer' 2>/dev/null || true
  sleep 1
  pkill -9 -f 'ambient-e2e-composer' 2>/dev/null || true
  local ids
  ids="$(docker ps -aq --filter "name=^${PREFIX}" 2>/dev/null || true)"
  [[ -n "$ids" ]] && docker rm -f $ids >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  echo "cleanup done (rc=$rc)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

mkdir -p "$WORK/audio" "$WORK/images" "$WORK/profiles" "$WORK/icecast" "$OUT"

# ------------------------------------------------------------------ test media
if [[ ! -f "$WORK/audio/a3.mp3" ]]; then
  log "generating audio"
  i=1
  for f in 330 550 880; do
    ff -loglevel error -y -f lavfi \
       -i "sine=frequency=${f}:sample_rate=44100:duration=30" \
       -af "aformat=channel_layouts=stereo,volume=-8dB" \
       -c:a libmp3lame -b:a 256k -ar 44100 -ac 2 "$WORK/audio/a${i}.mp3"
    i=$((i + 1))
  done
fi
if [[ ! -f "$WORK/images/img3.jpg" ]]; then
  log "generating images at three different source geometries"
  ff -loglevel error -y -f lavfi -i "gradients=s=1920x1080:c0=0x102030:c1=0x4FC3F7:n=2" \
     -frames:v 1 "$WORK/images/img1.jpg"
  ff -loglevel error -y -f lavfi -i "testsrc2=s=800x600" -frames:v 1 "$WORK/images/img2.jpg"
  ff -loglevel error -y -f lavfi -i "gradients=s=1280x720:c0=0x301020:c1=0xF7A34F:n=2" \
     -frames:v 1 "$WORK/images/img3.jpg"
fi
cat > "$WORK/profiles/img1.json" <<'JSON'
{"version":1,"source":"img1.jpg","extractor":"pillow-kmeans-v1",
 "dominant":"#102030","accent":"#4FC3F7","palette":["#102030","#4FC3F7"],
 "brightness":0.31,"warmth":-0.55,"mood":"cool"}
JSON
cat > "$WORK/profiles/img3.json" <<'JSON'
{"version":1,"source":"img3.jpg","extractor":"pillow-kmeans-v1",
 "dominant":"#301020","accent":"#F7A34F","palette":["#301020","#F7A34F"],
 "brightness":0.62,"warmth":0.71,"mood":"warm"}
JSON

# Lists in the exact contract formats: absolute in-container paths, one per line.
printf '/media/audio/%s\n' a1.mp3 a2.mp3 a3.mp3 > "$WORK/playlist.m3u"
: > "$WORK/images.list"
for n in img1.jpg img2.jpg img3.jpg; do printf '%s\n' "$WORK/images/$n" >> "$WORK/images.list"; done
ok "media + lists ready"

# --------------------------------------------------------------------- icecast
SOURCE_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
sed -e "s|@SOURCE_PASSWORD@|$SOURCE_PASSWORD|g" \
    -e "s|<mount-name>/live</mount-name>|<mount-name>/${CHANNEL}</mount-name>|" \
    -e "s|<fallback-mount>/fallback.mp3</fallback-mount>|<fallback-mount>/fallback.mp3</fallback-mount>|" \
    "$REPO/spikes/s1-liquidsoap-transport/icecast/icecast.xml.tmpl" \
    > "$WORK/icecast/icecast.xml"
chmod 644 "$WORK/icecast/icecast.xml"

docker network create "$NET" >/dev/null 2>&1 || true
log "starting icecast on 127.0.0.1:${ICE_PORT}"
docker run -d --name "${PREFIX}icecast" --network "$NET" \
  -v "$WORK/icecast/icecast.xml:/etc/icecast.xml:ro" \
  -p "127.0.0.1:${ICE_PORT}:8000" libretime/icecast:2.4.4 >/dev/null

log "starting mediamtx on 127.0.0.1:${RTMP_PORT} (rtmp) / ${HLS_PORT} (hls)"
docker run -d --name "${PREFIX}mediamtx" --network "$NET" \
  -e MTX_PROTOCOLS=tcp \
  -p "127.0.0.1:${RTMP_PORT}:1935" -p "127.0.0.1:${HLS_PORT}:8888" \
  bluenviron/mediamtx:1.9.3 >/dev/null

sleep 3

log "starting liquidsoap (channel.liq) as an icecast source client"
docker run -d --name "${PREFIX}liquidsoap" --network "$NET" \
  -v "$REPO/liquidsoap:/liq:ro" -v "$WORK:/media:ro" \
  -e CHANNEL_NAME="$CHANNEL" \
  -e PLAYLIST_FILE=/media/playlist.m3u \
  -e ICECAST_HOST="${PREFIX}icecast" -e ICECAST_PORT=8000 \
  -e CHANNEL_MOUNT="/${CHANNEL}" \
  -e ICECAST_SOURCE_PASSWORD="$SOURCE_PASSWORD" \
  -e CROSSFADE_SECONDS=3.0 \
  savonet/liquidsoap:v2.4.2 liquidsoap /liq/channel.liq >/dev/null

for _ in $(seq 1 40); do
  code="$(python3 -c "
import sys,urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:${ICE_PORT}/${CHANNEL}',timeout=2).read(1)
    print(200)
except Exception as e:
    print(getattr(e,'code',0))
" 2>/dev/null || echo 0)"
  [[ "$code" == 200 ]] && break
  sleep 0.5
done
[[ "$code" == 200 ]] || { docker logs "${PREFIX}liquidsoap" 2>&1 | tail -20; die "icecast mount /${CHANNEL} never answered 200"; }
ok "icecast mount /${CHANNEL} is live"

# -------------------------------------------------------------------- composer
export CHANNEL_NAME="$CHANNEL" WIDTH HEIGHT FPS PRODUCER_FPS
export AUDIO_URL="http://127.0.0.1:${ICE_PORT}/${CHANNEL}"
export RELAY_RTMP="rtmp://127.0.0.1:${RTMP_PORT}"
export PLUGIN_DIR="$REPO/plugins" HOT_SET=showfreqs-bars ACTIVE_PLUGIN=showfreqs-bars
export IMAGES_LIST="$WORK/images.list" HOLD_SECONDS=8 FADE_SECONDS=2
export RUN_DIR="$WORK/run" PROGRESS_FILE="$WORK/run/progress"
export PYTHON_BIN="$HOME/ambient-streamer/.venv/bin/python"
export SLIDESHOW_BIN="$REPO/ffmpeg/slideshow.py"
export ZMQ_BIND_HOST=127.0.0.1 ZMQ_BIND_PORT=5555
mkdir -p "$RUN_DIR"

log "starting composer for ${RUNTIME}s"
setsid bash -c 'exec -a ambient-e2e-composer "$0" "$@"' "$REPO/ffmpeg/entrypoint.sh" \
  > "$OUT/composer.log" 2>&1 &
COMPOSER_PID=$!

sleep 12
if ! kill -0 "$COMPOSER_PID" 2>/dev/null; then
  tail -40 "$OUT/composer.log"; die "composer died during startup"
fi
FF_PID="$(pgrep -f 'filter_complex_script' | head -1 || true)"
PROD_PID="$(pgrep -f 'slideshow.py' | head -1 || true)"
ok "composer up (ffmpeg pid=${FF_PID:-?} producer pid=${PROD_PID:-?})"

sleep 20
log "sampling the live RTMP output (full-res) and the preview"
ff -loglevel error -y -i "rtmp://127.0.0.1:${RTMP_PORT}/${CHANNEL}" -t 4 \
   -vf "signalstats,metadata=print:key=lavfi.signalstats.YMIN:file=$OUT/ymin.txt" \
   -f null - 2>"$OUT/read-main.log" || warn "main read failed"
ff -loglevel error -y -i "rtmp://127.0.0.1:${RTMP_PORT}/${CHANNEL}" \
   -frames:v 1 "$OUT/frame-main.png" 2>>"$OUT/read-main.log" || warn "main frame grab failed"
ff -loglevel error -y -i "rtmp://127.0.0.1:${RTMP_PORT}/${CHANNEL}/preview" \
   -frames:v 1 "$OUT/frame-preview.png" 2>"$OUT/read-preview.log" || warn "preview frame grab failed"

log "CPU sample over the run so far"
CPU_FF="$(ps -o pcpu= -p "${FF_PID:-1}" 2>/dev/null | tr -d ' ' || echo n/a)"
CPU_PROD="$(ps -o pcpu= -p "${PROD_PID:-1}" 2>/dev/null | tr -d ' ' || echo n/a)"

sleep "$((RUNTIME - 40))"

echo
echo "================= PHASE 1 E2E RESULTS ================="
echo "--- composer progress (last block) ---"
tail -20 "$WORK/run/progress" 2>/dev/null || echo "(no progress file)"
echo
echo "--- speed / drop / dup across the run ---"
grep -a '^speed=' "$WORK/run/progress" | tail -8 | tr '\n' ' '; echo
echo "drop_frames: $(grep -a '^drop_frames=' "$WORK/run/progress" | tail -1)"
echo "dup_frames:  $(grep -a '^dup_frames=' "$WORK/run/progress" | tail -1)"
echo
echo "--- measured CPU (% of one core, average over process lifetime) ---"
echo "ffmpeg composer : ${CPU_FF}"
echo "slideshow       : ${CPU_PROD}"
echo "host load       : $(cut -d' ' -f1-3 /proc/loadavg) over $(nproc) cores"
echo
echo "--- signalstats YMIN (plugin.md: correct branch ~16, corrupt branch 0) ---"
grep -a 'YMIN' "$OUT/ymin.txt" 2>/dev/null | tail -5 || echo "(none)"
echo
echo "--- rendered frames ---"
for f in "$OUT/frame-main.png" "$OUT/frame-preview.png"; do
  if [[ -f "$f" ]]; then
    ff -loglevel error -i "$f" -vf "signalstats,metadata=print" -f null - 2>&1 \
      | grep -aE 'YAVG|YMIN|YMAX' | head -3
    echo "  $(basename "$f") $(python3 -c "
from PIL import Image;im=Image.open('$f');print(im.size, 'colours=', len(im.convert('RGB').getcolors(maxcolors=1000000) or []))")"
  else
    warn "$(basename "$f") missing"
  fi
done
echo
echo "--- slideshow producer log ---"
grep -a 'slideshow event=' "$OUT/composer.log" | tail -8 || echo "(none)"
echo
echo "--- liquidsoap ---"
docker logs "${PREFIX}liquidsoap" 2>&1 | grep -aE 'event=|Connected|error' | tail -8 || true
echo "telnet status: $(docker exec "${PREFIX}liquidsoap" sh -c 'echo "ambient.status" | timeout 3 nc 127.0.0.1 1234' 2>/dev/null | head -2 | tr '\n' ' ' || echo unavailable)"
echo
echo "--- composer warnings/errors ---"
grep -aiE 'error|invalid|corrupt|Conversion failed' "$OUT/composer.log" | head -10 || echo "(none)"
echo "======================================================="
