#!/usr/bin/env bash
# Task 0 — verify reload_mode="watch" on a playlist FILE (not a directory).
#
# Three phases run concurrently, each in its own container + own work dir:
#   A  reload_mode="watch"    + atomic rename  <- what media-selection.md requires
#   B  reload_mode="watch"    + truncate-in-place rewrite
#   C  reload_mode="seconds"  + atomic rename   <- documented fallback
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/out"
MEDIA="$HERE/media"
LIQ_IMAGE="${LIQ_IMAGE:-savonet/liquidsoap:v2.4.2}"
PREFIX="t0-"
RUNTIME="${RUNTIME:-70}"
SWAP_AT="${SWAP_AT:-12}"
TRACK_SECS=8

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s %s\n' "$BLU" "$NC" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s[!!]%s %s\n' "$YLW" "$NC" "$*"; }
die()  { printf '%s[XX]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

cleanup() {
  local ids
  ids="$(docker ps -aq --filter "name=^${PREFIX}" 2>/dev/null || true)"
  [[ -n "$ids" ]] && docker rm -f $ids >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir -p "$OUT" "$MEDIA"

# ---- media ---------------------------------------------------------------
if [[ ! -f "$MEDIA/t6.mp3" ]]; then
  log "generating 6 test tracks (${TRACK_SECS}s, MP3 256k 44.1k stereo)"
  i=1
  for f in 220 330 440 550 660 880; do
    ffmpeg -nostdin -hide_banner -loglevel error -y \
      -f lavfi -i "sine=frequency=${f}:sample_rate=44100:duration=${TRACK_SECS}" \
      -af "aformat=channel_layouts=stereo" \
      -c:a libmp3lame -b:a 256k -ar 44100 -ac 2 \
      "$MEDIA/t${i}.mp3" </dev/null
    i=$((i + 1))
  done
fi
ok "media ready: $(ls "$MEDIA" | tr '\n' ' ')"

# ---- phases --------------------------------------------------------------
start_phase() {
  local phase="$1" mode="$2" secs="$3"
  local wd="$HERE/w-$phase"
  mkdir -p "$wd"
  printf '%s\n' "/media/t1.mp3" "/media/t2.mp3" "/media/t3.mp3" > "$wd/playlist.m3u"
  docker run -d --name "${PREFIX}${phase}" \
    -v "$HERE:/spike:ro" -v "$MEDIA:/media:ro" -v "$wd:/w" \
    -e PLAYLIST_FILE=/w/playlist.m3u \
    -e RELOAD_MODE="$mode" -e RELOAD_SECONDS="$secs" -e PHASE="$phase" \
    "$LIQ_IMAGE" liquidsoap /spike/watch.liq >/dev/null
}

swap_atomic() {
  local wd="$1"
  printf '%s\n' "/media/t4.mp3" "/media/t5.mp3" "/media/t6.mp3" > "$wd/.playlist.m3u.tmp"
  mv -f "$wd/.playlist.m3u.tmp" "$wd/playlist.m3u"   # same-dir rename(2), atomic
}

swap_inplace() {
  local wd="$1"
  printf '%s\n' "/media/t4.mp3" "/media/t5.mp3" "/media/t6.mp3" > "$wd/playlist.m3u"
}

log "starting phases A (watch), B (watch), C (seconds=10)"
start_phase A watch 10
start_phase B watch 10
start_phase C seconds 10

for p in A B C; do
  for _ in $(seq 1 40); do
    docker logs "${PREFIX}${p}" 2>&1 | grep -q T0_START && break
    sleep 0.5
  done
done
ok "all three liquidsoap instances up"

declare -A PID_BEFORE
for p in A B C; do
  PID_BEFORE[$p]="$(docker inspect -f '{{.State.Pid}}' "${PREFIX}${p}")"
done

log "letting them play for ${SWAP_AT}s before swapping the playlist"
sleep "$SWAP_AT"

SWAP_T="$(date +%s.%N)"
swap_atomic  "$HERE/w-A"
swap_inplace "$HERE/w-B"
swap_atomic  "$HERE/w-C"
ok "playlists swapped to t4/t5/t6 at $SWAP_T"

sleep "$((RUNTIME - SWAP_AT))"

for p in A B C; do
  docker logs "${PREFIX}${p}" > "$OUT/phase-$p.log" 2>&1 || true
done

echo
echo "================ TASK 0 RESULTS ================"
echo "swap wall clock: $SWAP_T"
for p in A B C; do
  pid_after="$(docker inspect -f '{{.State.Pid}}' "${PREFIX}${p}" 2>/dev/null || echo GONE)"
  echo
  echo "---------------- phase $p ----------------"
  case "$p" in
    A) echo "config: reload_mode=watch    swap=atomic rename" ;;
    B) echo "config: reload_mode=watch    swap=truncate in place" ;;
    C) echo "config: reload_mode=seconds10 swap=atomic rename" ;;
  esac
  if [[ "${PID_BEFORE[$p]}" == "$pid_after" && "$pid_after" != "GONE" ]]; then
    ok "same pid $pid_after — no restart"
  else
    warn "pid ${PID_BEFORE[$p]} -> $pid_after"
  fi
  echo "-- track timeline --"
  grep -a T0_TRACK "$OUT/phase-$p.log" | sed 's/.*T0_TRACK/T0_TRACK/' || echo "(none)"
  echo "-- reload evidence --"
  grep -aiE "reload|watch|inotify" "$OUT/phase-$p.log" | tail -8 || echo "(none)"
  if grep -aq "T0_TRACK.*t[456]\.mp3" "$OUT/phase-$p.log"; then
    ok "PICKED UP the new playlist"
  else
    warn "did NOT pick up the new playlist"
  fi
  echo "-- failures/underruns --"
  grep -aiE "underrun|failed|error|blank" "$OUT/phase-$p.log" | tail -5 || echo "(none)"
done
echo
echo "=============== END TASK 0 ==============="
