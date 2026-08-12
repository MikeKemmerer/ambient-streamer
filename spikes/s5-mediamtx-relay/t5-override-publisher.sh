#!/usr/bin/env bash
# Q5 — does overridePublisher let a restarted composer reclaim its path immediately?
# The interesting case is not a clean exit (the kernel closes that socket for us) but a
# composer that is wedged with its RTMP socket still open. SIGSTOP reproduces that.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$RESULTS_DIR/t5-override-publisher.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap 'kill -CONT $(jobs -p) 2>/dev/null || true; spike_cleanup' EXIT

CH="spike01"

publish_conns() { http_get "$RELAY_API/v3/rtmpconns/list" \
  | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin)["items"] if c["state"]=="publish"))'; }

run_case() { # run_case <label> <config> <expect-takeover: yes|no>
  local label="$1" cfg="$2" expect="$3"
  echo
  log "CASE $label (config: $(basename "$cfg"))"
  relay_down
  relay_up "$cfg"

  local a b
  a="$(publish_bed "$WORK_DIR/bed.mp4" "$RELAY_RTMP/$CH" "$WORK_DIR/t5-$label-A.log")"; track "$a"
  sleep 4
  echo "  A publishing, publish conns=$(publish_conns)"

  # Wedge A: process frozen, TCP socket still open. This is the stale session.
  kill -STOP "$a"
  echo "  A frozen with SIGSTOP (socket left open)"
  sleep 1

  local t0 t1 rc
  t0="$(now_ms)"
  b="$(publish_bed "$WORK_DIR/bed.mp4" "$RELAY_RTMP/$CH" "$WORK_DIR/t5-$label-B.log")"; track "$b"
  # Wait for B to actually own the path, capped so a refusal does not hang the case.
  local owned=no
  for i in $(seq 1 40); do
    if ! kill -0 "$b" 2>/dev/null; then break; fi
    if grep -qiE 'error|failed|denied' "$WORK_DIR/t5-$label-B.log" 2>/dev/null; then break; fi
    if [[ "$(http_get "$RELAY_API/v3/paths/get/$CH" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ready"])')" == "True" ]]; then
      sleep 0.25
      owned=yes; break
    fi
    sleep 0.25
  done
  t1="$(now_ms)"

  if kill -0 "$b" 2>/dev/null; then rc="alive"; else rc="exited"; fi
  echo "  B after $((t1-t0)) ms: process=$rc pathOwned=$owned publishConns=$(publish_conns)"
  echo "  B log: $(tail -2 "$WORK_DIR/t5-$label-B.log" | tr '\n' ' ')"
  kill -CONT "$a" 2>/dev/null || true
  sleep 1
  if kill -0 "$a" 2>/dev/null; then echo "  A after resume: still running"; else echo "  A after resume: exited (was disconnected)"; fi
  echo "  relay log:"
  docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" logs --no-color relay 2>&1 | grep -E 'is publishing|closed|ERR' | tail -6 | sed 's/^/    /'
  kill_tracked
  CASE_RESULT="$rc/$owned"
}

ensure_beds

run_case "override-yes" "$SPIKE_DIR/../../docker/mediamtx.yml" yes
YES_RESULT="$CASE_RESULT"

# Control: same config with the one key flipped, to prove the key is what makes it work.
sed 's/^  overridePublisher: yes/  overridePublisher: no/' "$SPIKE_DIR/../../docker/mediamtx.yml" \
  > "$WORK_DIR/mediamtx-override-no.yml"
grep -n 'overridePublisher' "$WORK_DIR/mediamtx-override-no.yml"
run_case "override-no" "$WORK_DIR/mediamtx-override-no.yml" no
NO_RESULT="$CASE_RESULT"

relay_down
echo
echo "override-yes: B=$YES_RESULT   override-no: B=$NO_RESULT   (want alive/yes then exited or alive/no)"
if [[ "$YES_RESULT" == "alive/yes" && "$NO_RESULT" != "alive/yes" ]]; then
  pass_fail PASS Q5 "overridePublisher:yes reclaims a wedged path; with :no the takeover is refused"
else
  pass_fail FAIL Q5 "yes-case=$YES_RESULT no-case=$NO_RESULT"
fi
