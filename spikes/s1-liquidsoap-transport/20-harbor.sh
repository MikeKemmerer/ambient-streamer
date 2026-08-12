#!/usr/bin/env bash
# Q1/B + Q2: HTTP harbor. Restart survival and the measured audio gap.
# Runs twice: with and without -use_wallclock_as_timestamps, because that flag
# decides whether the outage becomes inserted silence or permanent A/V skew.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

PRE=20; DOWN="${DOWN:-6}"; POST=30
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"

# -reconnect_delay_max is the GIVE-UP threshold, not a per-attempt cap: FFmpeg
# backs off 0,1,3,7,15,... seconds and aborts once the next delay would exceed
# it. At 5 that is a ~4 s retry window, shorter than any container restart.
RECONNECT=(-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
           -reconnect_on_network_error 1 -reconnect_delay_max 120)

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-harbor; }
trap cleanup EXIT

log "starting liquidsoap harbor on 127.0.0.1:${HARBOR_PORT}"
liq_run liq-harbor /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || { docker logs "${CONTAINER_PREFIX}liq-harbor" 2>&1 | tail -20; die "harbor mount never answered 200"; }
ok "harbor mount answering"

for MODE in wallclock nowallclock; do
  RAW="$OUT_DIR/harbor-$MODE.raw"; FLOG="$OUT_DIR/harbor-$MODE.ffmpeg.log"
  PROG="$OUT_DIR/harbor-$MODE.progress"; EV="$OUT_DIR/harbor-$MODE.events"
  : > "$PROG"; : > "$EV"
  WC=(); [[ "$MODE" == wallclock ]] && WC=(-use_wallclock_as_timestamps 1)

  log "=== mode=$MODE ==="
  echo "ffmpeg_start $(now)" >> "$EV"
  ff -loglevel level+info -progress "$PROG" -stats_period 1 \
     "${WC[@]}" "${RECONNECT[@]}" \
     -i "$URL" \
     -af "aresample=44100:async=1000:first_pts=0" \
     -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$RAW" >"$FLOG" 2>&1 &
  FF_PID=$!
  sleep "$PRE"
  alive "$FF_PID" || { tail -30 "$FLOG"; die "ffmpeg died during normal operation"; }
  T1="$(progress_out_ms "$PROG")"; ok "before kill: out_time_ms=$T1"

  KILL_T="$(now)"; echo "liq_kill $KILL_T" >> "$EV"
  docker kill "${CONTAINER_PREFIX}liq-harbor" >/dev/null 2>&1 || true
  sleep "$DOWN"
  alive "$FF_PID" && ok "ffmpeg alive ${DOWN}s into the outage" || warn "ffmpeg EXITED during the outage"
  docker start "${CONTAINER_PREFIX}liq-harbor" >/dev/null
  UP_T="$(now)"; echo "liq_restart $UP_T" >> "$EV"
  wait_harbor && echo "harbor_ready $(now)" >> "$EV"
  sleep "$POST"

  T2="$(progress_out_ms "$PROG")"
  if alive "$FF_PID"; then ok "RESULT[$MODE]: ffmpeg ALIVE, out_time_ms ${T1} -> ${T2}"
  else warn "RESULT[$MODE]: ffmpeg DEAD, out_time_ms stopped at ${T2}"; fi
  kill_pid "$FF_PID"; unset FF_PID

  echo "---- reconnect lines ----"
  grep -aiE "reconnect|Connection|error|Invalid|End of file" "$FLOG" | tail -15 || echo "(none)"
  echo "---- audio analysis ----"
  "$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$RAW" || true
done

docker logs "${CONTAINER_PREFIX}liq-harbor" > "$OUT_DIR/harbor.liq.log" 2>&1 || true
