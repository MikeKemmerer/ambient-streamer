#!/usr/bin/env bash
# Tear down everything this spike created. Touches only the ambient-spike compose project,
# containers named ambient-spike-*, and ffmpeg processes tagged by this spike.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

log "killing spike ffmpeg processes"
pkill -f 'ffmpeg.*ambient-spike-tag' 2>/dev/null || true
pkill -f 'poll-path.py' 2>/dev/null || true
sleep 1
pkill -9 -f 'ffmpeg.*ambient-spike-tag' 2>/dev/null || true

log "removing compose project '$PROJECT'"
docker compose -p "$PROJECT" -f "$SPIKE_DIR/docker-compose.yml" down -v --remove-orphans || true

for c in ambient-spike-cfgcheck ambient-spike-neg; do
  docker rm -f "$c" >/dev/null 2>&1 || true
done

log "remaining containers on this host:"
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Status}}'
log "networks matching ambient:"
docker network ls --filter name=ambient --format '  {{.Name}}' || true
log "volumes matching ambient:"
docker volume ls --filter name=ambient --format '  {{.Name}}' || true
ok "cleanup done"
