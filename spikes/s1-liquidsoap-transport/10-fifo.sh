#!/usr/bin/env bash
# Q1/A: named pipe. Does FFmpeg survive a hard Liquidsoap restart?
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

FIFO_DIR="$SPIKE_DIR/fifo"; FIFO="$FIFO_DIR/audio.fifo"; FIFO_IN_CTR="/spike/fifo/audio.fifo"
RAW="$OUT_DIR/fifo.raw"; FLOG="$OUT_DIR/fifo.ffmpeg.log"
PROG="$OUT_DIR/fifo.progress"; EV="$OUT_DIR/fifo.events"
PRE=20; DOWN=5; POST=25

cleanup() { kill_pid "${FF_PID:-}"; docker rm -f "${CONTAINER_PREFIX}liq-fifo" >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$FIFO_DIR"; : > "$EV"; : > "$PROG"
# A fifo has exactly one useful reader; a leftover reader from an aborted run
# silently steals bytes and corrupts the stream for everyone.
pkill -f "ffmpeg.*audio\.fifo" 2>/dev/null || true
rm -f "$FIFO" 2>/dev/null || true
# 0666: the Liquidsoap image runs as its own uid, which cannot write a fifo owned
# by the host user under the default mode.
mkfifo -m 666 "$FIFO"

log "starting ffmpeg reader on the fifo (it blocks until a writer opens)"
echo "ffmpeg_start $(now)" >> "$EV"
ff -loglevel level+info -progress "$PROG" -stats_period 1 \
   -use_wallclock_as_timestamps 1 -f mp3 -i "$FIFO" \
   -af "aresample=44100:async=1000:first_pts=0" \
   -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
FF_PID=$!
sleep 2
alive "$FF_PID" && ok "ffmpeg pid $FF_PID blocked on fifo open, not exited" \
                || warn "ffmpeg already gone before any writer appeared"

log "starting liquidsoap writer"
liq_run liq-fifo /spike/liq/fifo.liq -e FIFO_PATH="$FIFO_IN_CTR"
echo "liq_start $(now)" >> "$EV"
sleep 5
[[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER_PREFIX}liq-fifo")" == "true" ]] \
  || { docker logs "${CONTAINER_PREFIX}liq-fifo" 2>&1 | tail -20; die "liquidsoap writer exited immediately"; }
sleep "$PRE"
alive "$FF_PID" || die "ffmpeg died during normal operation"
T1="$(progress_out_ms "$PROG")"; ok "before kill: out_time_ms=$T1"

log "SIGKILLing the liquidsoap container"
echo "liq_kill $(now)" >> "$EV"
docker kill "${CONTAINER_PREFIX}liq-fifo" >/dev/null 2>&1 || true
sleep "$DOWN"
if alive "$FF_PID"; then warn "ffmpeg still alive ${DOWN}s after writer died"
else warn "ffmpeg EXITED ${DOWN}s after writer died"; fi

log "restarting liquidsoap"
docker start "${CONTAINER_PREFIX}liq-fifo" >/dev/null
echo "liq_restart $(now)" >> "$EV"
sleep "$POST"

T2="$(progress_out_ms "$PROG")"
if alive "$FF_PID"; then
  ok "RESULT: ffmpeg alive after restart; out_time_ms ${T1} -> ${T2}"
else
  warn "RESULT: ffmpeg is DEAD after the restart cycle; out_time_ms stopped at ${T2}"
fi

kill_pid "$FF_PID"; unset FF_PID
docker logs "${CONTAINER_PREFIX}liq-fifo" > "$OUT_DIR/fifo.liq.log" 2>&1 || true
echo "---- ffmpeg tail ----"; tail -20 "$FLOG"
echo "---- exit reason ----"; grep -aiE "end of file|error|Exiting|No more output" "$FLOG" | tail -10 || true
