#!/usr/bin/env bash
# ambient-streamer — start, stop and inspect one channel's compose project.
#
# The reason this script exists: `docker compose` resolves the implicit .env
# relative to the COMPOSE FILE's directory. A per-channel compose file lives in
# channels/<name>/, so compose finds only that channel's .env and never the root
# one — every ${ICECAST_SOURCE_PASSWORD} in the template resolves to empty and
# the channel comes up mute against a relay that rejects it. Both files have to
# be named explicitly, in this order:
#
#   docker compose --env-file .env --env-file channels/<name>/.env \
#                  -f channels/<name>/docker-compose.yml ...
#
# Root first so the channel file wins on any key they share.
#
# The stream key is not this script's business. It is read at runtime by the
# relay, straight out of channels/<name>/.env — see docker/publish-youtube.sh.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -t 2 ]]; then
	C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
	C_RED=''; C_GRN=''; C_YLW=''; C_DIM=''; C_OFF=''
fi
log()  { printf '%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*" >&2; }
ok()   { printf '%s ok %s %s\n' "$C_GRN" "$C_OFF" "$*" >&2; }
warn() { printf '%swarn%s %s\n' "$C_YLW" "$C_OFF" "$*" >&2; }
die()  { printf '%sfail%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
	cat >&2 <<-EOF
	usage: scripts/channel.sh <command> <channel> [extra docker compose args]

	  start     bring the channel's two containers up
	  stop      stop and remove them (this project only — never the global stack)
	  restart   stop then start
	  status    container state for this channel
	  logs      follow both containers
	  config    print the fully resolved compose file
	EOF
	exit 2
}

[[ $# -ge 2 ]] || usage
CMD="$1"; CHANNEL="$2"; shift 2

[[ "$CHANNEL" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] \
	|| die "'$CHANNEL' is not a channel name (^[a-z0-9][a-z0-9-]{0,30}\$)"

CHANNEL_DIR="$REPO_ROOT/channels/$CHANNEL"
CHANNEL_ENV="$CHANNEL_DIR/.env"
COMPOSE_FILE="$CHANNEL_DIR/docker-compose.yml"
ROOT_ENV="$REPO_ROOT/.env"

[[ -d "$CHANNEL_DIR" ]] || die "no such channel: channels/$CHANNEL"
[[ -f "$ROOT_ENV" ]]    || die "missing $ROOT_ENV — copy .env.example and fill it in"
[[ -f "$CHANNEL_ENV" ]] || die "missing $CHANNEL_ENV — copy channels/example/.env.example"
[[ -f "$COMPOSE_FILE" ]] || die "missing $COMPOSE_FILE — run: python -m ambient.compile $CHANNEL"

# The generated compose file carries absolute host paths, so a file copied from
# another machine points at directories that do not exist here.
if ! grep -q "$REPO_ROOT" "$COMPOSE_FILE"; then
	warn "$COMPOSE_FILE does not reference $REPO_ROOT"
	warn "it was probably generated elsewhere; re-run: python -m ambient.compile $CHANNEL"
fi

compose() {
	docker compose \
		--project-name "ambient-$CHANNEL" \
		--env-file "$ROOT_ENV" \
		--env-file "$CHANNEL_ENV" \
		--file "$COMPOSE_FILE" \
		"$@"
}

require_stack() {
	docker network inspect ambient >/dev/null 2>&1 \
		|| die "the 'ambient' network is missing — start the global stack first: docker compose -p ambient up -d"
}

case "$CMD" in
	start)
		require_stack
		grep -qE '^[[:space:]]*YOUTUBE_STREAM_KEY[[:space:]]*=[[:space:]]*[^[:space:]]' "$CHANNEL_ENV" \
			|| warn "YOUTUBE_STREAM_KEY is empty in $CHANNEL_ENV — the relay will stay off YouTube"
		log "starting ambient-$CHANNEL"
		compose up -d "$@"
		ok "ambient-$CHANNEL up"
		;;
	stop)
		# Scoped to this project by --project-name. It cannot reach the global
		# stack, and it cannot reach anything else on the host.
		log "stopping ambient-$CHANNEL"
		compose down "$@"
		ok "ambient-$CHANNEL down"
		;;
	restart)
		"${BASH_SOURCE[0]}" stop "$CHANNEL"
		"${BASH_SOURCE[0]}" start "$CHANNEL"
		;;
	status) compose ps "$@" ;;
	logs)   compose logs --follow --tail 100 "$@" ;;
	config) compose config "$@" ;;
	*)      usage ;;
esac
