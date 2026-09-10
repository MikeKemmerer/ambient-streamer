#!/usr/bin/env bash
# Q6: FFmpeg starts before Liquidsoap is listening. Compose starts both at once,
# so the composer must tolerate a refused connection on its audio input.
# -reconnect_max_retries does not exist in FFmpeg 6.1, so it is not tested here.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

LEAD="${LEAD:-15}"; AFTER="${AFTER:-25}"
HARBOR_PORT="${SPIKE_PORT:-18092}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
SUMMARY="$OUT_DIR/startup.summary.txt"; : > "$SUMMARY"

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-start; }
trap cleanup EXIT

run_case() {
  local name="$1"; shift
  local flog="$OUT_DIR/startup-$name.ffmpeg.log" prog="$OUT_DIR/startup-$name.progress"
  local raw="$OUT_DIR/startup-$name.raw"
  : > "$prog"
  rm_ctr liq-start

  log "=== case=$name : ffmpeg first, harbor down ==="
  ff -loglevel level+info -progress "$prog" -stats_period 1 "$@" -i "$URL" \
     -af "aresample=44100:async=1000:first_pts=0" \
     -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$raw" >"$flog" 2>&1 &
  FF_PID=$!
  sleep "$LEAD"
  local survived_lead="no"; alive "$FF_PID" && survived_lead="yes"

  log "bringing harbor up"
  liq_run liq-start /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
  local t_up; t_up="$(now)"
  local ttfa="never"
  for _ in $(seq 1 $((AFTER * 10))); do
    [[ -s "$raw" ]] && { ttfa="$(echo "$(now) - $t_up" | bc)"; break; }
    sleep 0.1
  done
  sleep 5
  local final="dead"; alive "$FF_PID" && final="alive"
  kill_pid "$FF_PID"; unset FF_PID
  rm_ctr liq-start

  printf '%-22s waited_for_harbor=%-4s ended=%-6s first_audio_after_harbor_up=%ss\n' \
    "$name" "$survived_lead" "$final" "$ttfa" | tee -a "$SUMMARY"
  grep -aiE "Connection refused|reconnect|Error opening|Invalid data" "$flog" \
    | head -4 | sed 's/^/    /' | tee -a "$SUMMARY" || true
}

run_case "plain-reconnect" \
  -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 120
run_case "on-network-error" \
  -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 \
  -reconnect_on_network_error 1 -reconnect_delay_max 120

echo; ok "summary:"; cat "$SUMMARY"
