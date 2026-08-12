#!/usr/bin/env bash
# Q1 — does docker/mediamtx.yml load on the real binary with no rejected keys?
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CFG="$(realpath "${1:-$SPIKE_DIR/../../docker/mediamtx.yml}")"
OUT="$RESULTS_DIR/t1-config-load.txt"
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
trap spike_cleanup EXIT INT TERM

log "config under test: $CFG"
log "image: $MTX_IMAGE"
docker image inspect "$MTX_IMAGE" --format 'digest: {{index .RepoDigests 0}}'

# Pull the shipped default config straight out of the image: that is the authoritative key
# list for this exact binary.
cid="$(docker create "$MTX_IMAGE")"
docker cp "$cid:/mediamtx.yml" "$WORK_DIR/default-mediamtx.yml" >/dev/null
docker rm "$cid" >/dev/null

top_keys() { grep -oE '^[a-zA-Z][a-zA-Z0-9_]*:' "$1" | tr -d ':' | sort -u; }
sub_keys() { grep -oE '^  [a-zA-Z][a-zA-Z0-9_]*:' "$1" | tr -d ' :' | sort -u; }

log "top-level keys in repo config that the shipped default does not know:"
comm -23 <(top_keys "$CFG") <(top_keys "$WORK_DIR/default-mediamtx.yml") | sed 's/^/    UNKNOWN-TOP: /' || true
log "nested keys in repo config that the shipped default does not know:"
comm -23 <(sub_keys "$CFG") <(sub_keys "$WORK_DIR/default-mediamtx.yml") | sed 's/^/    UNKNOWN-SUB: /' || true

# The real verdict: boot the binary with this config. No published ports, so this cannot
# collide with anything already on the host.
name="ambient-spike-cfgcheck"
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" -v "$CFG:/mediamtx.yml:ro" "$MTX_IMAGE" >/dev/null
sleep 4
running="$(docker inspect -f '{{.State.Running}}' "$name")"
exitcode="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
echo "--- container log ---"
docker logs "$name" 2>&1
echo "--- end log ---"
docker rm -f "$name" >/dev/null

echo "running=$running exitcode=$exitcode"
if [[ "$running" == "true" ]]; then
  pass_fail PASS Q1 "config accepted by $MTX_IMAGE; process alive after 4s"
else
  pass_fail FAIL Q1 "config rejected by $MTX_IMAGE; exit=$exitcode (see $OUT)"
  exit 1
fi
