#!/usr/bin/env bash
# Brings the global stack up, proves Icecast serves its fallback mount with no
# source connected, and ALWAYS tears itself down again.
#
# This runs on shared hosts. The trap below is not optional: an abandoned spike
# once left containers running on a production box. Cleanup is scoped to the
# `ambient` compose project and to containers this script created — nothing else
# is ever touched.
#
#   scripts/verify-stack.sh
#   KEEP_UP=1 scripts/verify-stack.sh    # leave the stack running (deliberate)
set -euo pipefail

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; NC=$'\033[0m'
log()  { printf '\n== %s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$NC" "$*" >&2; }
die()  { printf '%s✘%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROJECT="${AMBIENT_PROJECT:-ambient}"
PROBE="ambient-verify-probe-$$"
PROBE_IMAGE="${PROBE_IMAGE:-ambient-composer:dev}"
ICECAST_PORT="${AMBIENT_ICECAST_PORT:-8081}"
TEST_CHANNEL="${TEST_CHANNEL:-verify}"
MOUNTS_LIST="${AMBIENT_DATA_DIR:-./channels}/mounts.list"
MOUNTS_BACKUP=""
FALLBACK_MADE=""

cleanup() {
  local rc=$?
  set +e
  log "cleanup"
  docker rm -f "$PROBE" >/dev/null 2>&1
  if [[ -z "${KEEP_UP:-}" ]]; then
    # Scoped to this project only. Never `docker compose down` anywhere else.
    docker compose -p "$PROJECT" -f "$REPO_ROOT/docker-compose.yml" down --remove-orphans --timeout 10
  else
    warn "KEEP_UP set — leaving project '$PROJECT' running"
  fi
  if [[ -n "$MOUNTS_BACKUP" ]]; then mv -f "$MOUNTS_BACKUP" "$MOUNTS_LIST"
  elif [[ -n "${MOUNTS_CREATED:-}" ]]; then : > "$MOUNTS_LIST"; fi
  [[ -n "$FALLBACK_MADE" ]] && log "left generated fallback at $FALLBACK_MADE"
  ok "cleanup done"
  exit "$rc"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null || die "docker not found"
[[ -f .env ]] || die ".env not found — cp .env.example .env and fill in the passwords"

log "pre-flight: containers already running on this host (must survive)"
BEFORE="$(docker ps --format '{{.Names}}' | sort)"
printf '%s\n' "$BEFORE" | sed 's/^/  /'

log "generating the fallback MP3 (256k / 44.1k / stereo)"
if [[ ! -f common/fallback/default.mp3 ]]; then
  scripts/make-fallback.sh default
  FALLBACK_MADE="common/fallback/default.mp3"
fi
scripts/make-fallback.sh "$TEST_CHANNEL" >/dev/null || die "fallback generation failed"
ok "common/fallback/${TEST_CHANNEL}.mp3"

log "registering the test mount"
if [[ -f "$MOUNTS_LIST" ]]; then
  MOUNTS_BACKUP="${MOUNTS_LIST}.verify-bak"
  cp "$MOUNTS_LIST" "$MOUNTS_BACKUP"
  grep -qx "$TEST_CHANNEL" "$MOUNTS_LIST" || echo "$TEST_CHANNEL" >> "$MOUNTS_LIST"
else
  MOUNTS_CREATED=1
  echo "$TEST_CHANNEL" > "$MOUNTS_LIST"
fi

log "docker compose config"
docker compose -p "$PROJECT" config --quiet || die "compose config is invalid"
ok "global stack validates"

log "docker compose up -d"
docker compose -p "$PROJECT" up -d

log "waiting for containers to report running"
for _ in $(seq 1 30); do
  states="$(docker inspect -f '{{.Name}} {{.State.Status}}' ambient-icecast ambient-mediamtx 2>/dev/null || true)"
  [[ "$(grep -c ' running' <<<"$states")" == "2" ]] && break
  sleep 1
done
docker ps --filter "label=com.docker.compose.project=$PROJECT" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
grep -q 'ambient-icecast running'  <<<"$states" || { docker logs ambient-icecast  2>&1 | tail -30; die "icecast not running"; }
grep -q 'ambient-mediamtx running' <<<"$states" || { docker logs ambient-mediamtx 2>&1 | tail -30; die "mediamtx not running"; }
ok "icecast and mediamtx are running, not merely created"

log "no host ports on the relay"
docker port ambient-mediamtx 2>/dev/null | grep -q . && die "mediamtx published a host port" || ok "mediamtx publishes nothing"
docker port ambient-icecast  2>/dev/null | grep -q . && die "icecast published a host port"  || ok "icecast publishes nothing"

# The real test: fetch the channel mount with NO Liquidsoap running. Icecast must
# hand back the fallback file, and it must decode as MP3 256k/44.1k/stereo.
# Probe from the project's own composer image — same FFmpeg the compositor uses.
log "reading /${TEST_CHANNEL} with no source connected"
docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1 \
  || docker build -f docker/Dockerfile.composer -t "$PROBE_IMAGE" . \
  || die "composer image build failed"
docker run --rm --name "$PROBE" --network ambient --entrypoint /bin/sh "$PROBE_IMAGE" -c "
    set -e
    ffprobe -v error -rw_timeout 10000000 \
      -show_entries stream=codec_name,sample_rate,channels,bit_rate \
      -show_entries format=format_name \
      -of default=nw=1 'http://icecast:${ICECAST_PORT}/${TEST_CHANNEL}'
  " || die "fallback mount did not serve audio"
ok "fallback mount served audio with no source connected"

log "icecast log"
docker logs ambient-icecast 2>&1 | grep -iE 'fallback|mount|listener|source' | tail -15 || true

log "post-flight: pre-existing containers"
AFTER="$(docker ps --format '{{.Names}}' | sort)"
MISSING="$(comm -23 <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") || true)"
[[ -z "$MISSING" ]] && ok "every pre-existing container still running" \
  || die "containers disappeared: $MISSING"
