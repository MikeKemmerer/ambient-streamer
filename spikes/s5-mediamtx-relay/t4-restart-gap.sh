#!/usr/bin/env bash
# Q4 — THE key question. What happens to a long-lived downstream consumer when the composer
# (publisher) is replaced? The measurement is taken on youtube-sim, because that is the leg
# whose continuity the architecture is actually buying.
#
# Matrix:
#   A  bare consumer   + SIGKILL publisher, 2s restart   (naive pusher, worst case)
#   B  supervised loop + SIGKILL publisher, 2s restart   (what we would ship)
#   C  bare consumer   + make-before-break takeover      (does overridePublisher spare readers?)
#   D  fast-probe loop + make-before-break takeover      (best achievable gap)
#
# Variants are selected per invocation so one run stays inside the spike time budget:
#   ./t4-restart-gap.sh A C
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

VARIANTS=("${@:-A}")
OUT="$RESULTS_DIR/t4-restart-gap.txt"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT INT TERM

relay_up
ensure_beds
CH="spike01"
YTPATH="live"
WINDOW=16

start_consumer() { # start_consumer <mode> <logfile> -> pid
  case "$1" in
    bare)
      ffmpeg -nostdin -hide_banner -loglevel warning -i "$RELAY_RTMP/$CH" \
        -c copy -f flv -metadata comment=ambient-spike-tag "$YT_RTMP/$YTPATH" \
        </dev/null >"$2" 2>&1 & echo $! ;;
    loop)
      bash -c "while true; do ffmpeg -nostdin -hide_banner -loglevel warning \
        -i '$RELAY_RTMP/$CH' -c copy -f flv -metadata comment=ambient-spike-tag \
        '$YT_RTMP/$YTPATH' </dev/null; sleep 0.2; done" >"$2" 2>&1 & echo $! ;;
    loop-fast)
      bash -c "while true; do ffmpeg -nostdin -hide_banner -loglevel warning \
        -fflags nobuffer -analyzeduration 500000 -probesize 250000 \
        -i '$RELAY_RTMP/$CH' -c copy -f flv -metadata comment=ambient-spike-tag \
        '$YT_RTMP/$YTPATH' </dev/null; sleep 0.1; done" >"$2" 2>&1 & echo $! ;;
  esac
}

wait_ready() { # wait_ready <api> <path> <timeout-s>
  local deadline=$(( $(date +%s) + $3 ))
  while [[ $(date +%s) -lt $deadline ]]; do
    [[ "$(http_get "$1/v3/paths/get/$2" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["ready"])' 2>/dev/null)" == "True" ]] && return 0
    sleep 0.2
  done
  return 1
}

run_variant() { # run_variant <name> <consumer-mode> <break-mode>
  local name="$1" cmode="$2" bmode="$3"
  local marks="$WORK_DIR/t4$name.marks"; : > "$marks"
  echo
  log "VARIANT $name — consumer=$cmode break=$bmode"

  local pubA pubB cons pollY pollR
  pubA="$(publish_bed "$WORK_DIR/bed.mp4" "$RELAY_RTMP/$CH" "$WORK_DIR/t4$name-pubA.log")"; track "$pubA"
  wait_ready "$RELAY_API" "$CH" 15 || die "publisher never became ready on the relay"
  cons="$(start_consumer "$cmode" "$WORK_DIR/t4$name-consumer.log")"; track "$cons"
  wait_ready "$YT_API" "$YTPATH" 20 || warn "consumer never reached youtube-sim"

  python3 "$SPIKE_DIR/poll-path.py" "$YT_API"    "$YTPATH" "$WORK_DIR/t4$name-yt.csv"    "$WINDOW" 100 & pollY=$!; track "$pollY"
  python3 "$SPIKE_DIR/poll-path.py" "$RELAY_API" "$CH"     "$WORK_DIR/t4$name-relay.csv" "$WINDOW" 100 & pollR=$!; track "$pollR"
  sleep 5

  if [[ "$bmode" == "kill" ]]; then
    echo "break_kill $(now_ms)" >> "$marks"
    kill -9 "$pubA" 2>/dev/null || true
    sleep 2
    pubB="$(publish_bed "$WORK_DIR/bed.mp4" "$RELAY_RTMP/$CH" "$WORK_DIR/t4$name-pubB.log")"; track "$pubB"
    echo "composer_relaunched $(now_ms)" >> "$marks"
  else
    echo "break_takeover $(now_ms)" >> "$marks"
    pubB="$(publish_bed "$WORK_DIR/bed.mp4" "$RELAY_RTMP/$CH" "$WORK_DIR/t4$name-pubB.log")"; track "$pubB"
    sleep 3
    kill -9 "$pubA" 2>/dev/null || true
    echo "old_composer_killed $(now_ms)" >> "$marks"
  fi

  sleep 4
  local alive="DEAD"
  kill -0 "$cons" 2>/dev/null && alive="ALIVE"
  echo "  consumer process after the break: $alive"
  echo "  consumer log: $(grep -iE 'error|EOF|Input/output' "$WORK_DIR/t4$name-consumer.log" | tail -2 | tr '\n' ' ')"

  wait "$pollY" 2>/dev/null || true
  wait "$pollR" 2>/dev/null || true
  pkill -9 -P "$cons" 2>/dev/null || true
  kill_tracked
  sleep 2

  python3 "$SPIKE_DIR/analyze-gap.py" "$WORK_DIR/t4$name-relay.csv" "$name relay side"   "$marks"
  python3 "$SPIKE_DIR/analyze-gap.py" "$WORK_DIR/t4$name-yt.csv"    "$name YOUTUBE side" "$marks"
  eval "RESULT_$name='$alive'"
}

for v in "${VARIANTS[@]}"; do
  case "$v" in
    A) run_variant A bare      kill ;;
    B) run_variant B loop      kill ;;
    C) run_variant C bare      takeover ;;
    D) run_variant D loop-fast takeover ;;
    *) die "unknown variant '$v'" ;;
  esac
done

echo
echo "--- relay log (publisher/reader lifecycle) ---"
docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" logs --no-color relay 2>&1 \
  | grep -E 'is publishing|is reading|closing existing|closed:' | tail -30
