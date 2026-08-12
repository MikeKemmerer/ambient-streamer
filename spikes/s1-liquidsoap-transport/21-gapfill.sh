#!/usr/bin/env bash
# Q2 follow-up: after FFmpeg reconnects, is the outage filled with silence (audio
# time keeps tracking wallclock) or simply dropped (audio time falls permanently
# behind video)? Compares input-timestamp / aresample variants.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_media

PRE=15; DOWN="${DOWN:-8}"; POST=25
HARBOR_PORT="${SPIKE_PORT:-18096}"
URL="http://127.0.0.1:${HARBOR_PORT}/${HARBOR_MOUNT}"
PY="${PY:-$HOME/ambient-streamer/.venv/bin/python}"
RECONNECT=(-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
           -reconnect_on_network_error 1 -reconnect_delay_max 120)
SUMMARY="$OUT_DIR/gapfill.summary.txt"; : > "$SUMMARY"

cleanup() { kill_pid "${FF_PID:-}"; rm_ctr liq-gap; }
trap cleanup EXIT

rm_ctr liq-gap
liq_run liq-gap /spike/liq/harbor.liq -p "127.0.0.1:${HARBOR_PORT}:${HARBOR_PORT}"
wait_harbor || die "harbor mount never answered 200"
ok "harbor up"

run_variant() {
  local name="$1" wc="$2" af="$3"
  local raw="$OUT_DIR/gap-$name.raw" flog="$OUT_DIR/gap-$name.ffmpeg.log"
  local wcflag=(); [[ "$wc" == "1" ]] && wcflag=(-use_wallclock_as_timestamps 1)

  log "=== variant=$name ==="
  ff -loglevel level+warning "${wcflag[@]}" "${RECONNECT[@]}" -i "$URL" \
     -af "$af" -c:a pcm_s16le -ar 44100 -ac 2 -f s16le -y "$raw" >"$flog" 2>&1 &
  FF_PID=$!

  local t_first=""
  for _ in $(seq 1 300); do [[ -s "$raw" ]] && { t_first="$(now)"; break; }; sleep 0.1; done
  [[ -n "$t_first" ]] || { kill_pid "$FF_PID"; unset FF_PID; warn "$name: no audio at all"; return; }

  sleep "$PRE"
  docker kill "${CONTAINER_PREFIX}liq-gap" >/dev/null 2>&1 || true
  local t_kill; t_kill="$(now)"
  sleep "$DOWN"
  docker start "${CONTAINER_PREFIX}liq-gap" >/dev/null
  wait_harbor || true
  local t_ready; t_ready="$(now)"
  sleep "$POST"
  local t_stop; t_stop="$(now)"
  local surv="ALIVE"; alive "$FF_PID" || surv="DEAD"
  kill_pid "$FF_PID"; unset FF_PID

  local audio_s wall_s outage_s
  audio_s="$(python3 -c "import os;print(f'{os.path.getsize(\"$raw\")/4/44100:.2f}')")"
  wall_s="$(echo "$t_stop - $t_first" | bc)"
  outage_s="$(echo "$t_ready - $t_kill" | bc)"
  printf '%-14s survived=%-5s audio=%ss wall=%.2fs deficit=%.2fs source_outage=%.2fs\n' \
    "$name" "$surv" "$audio_s" "$wall_s" "$(echo "$wall_s - $audio_s" | bc)" "$outage_s" \
    | tee -a "$SUMMARY"
  "$PY" "$SPIKE_DIR/analyze/audio_probe.py" "$raw" > "$OUT_DIR/gap-$name.probe.json" 2>&1 || true
  "$PY" - "$OUT_DIR/gap-$name.probe.json" <<'EOF' | tee -a "$SUMMARY"
import json, sys
d = json.load(open(sys.argv[1]))
print(f"    inserted_silence_runs={[r['duration_s'] for r in d.get('silence_runs', [])]}")
EOF
}

run_variant "wc-async1000" 1 "aresample=44100:async=1000:first_pts=0"
run_variant "wc-async1"    1 "aresample=44100:async=1:first_pts=0"
run_variant "nowc-async1000" 0 "aresample=44100:async=1000:first_pts=0"

echo; ok "summary:"; cat "$SUMMARY"
