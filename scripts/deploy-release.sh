#!/usr/bin/env bash
# Pull immutable release images and optionally roll only backend/Liquidsoap.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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
	usage: scripts/deploy-release.sh <release.env> [--backend] [--liquidsoap <channel>]...

	Pulls the release's immutable backend and Liquidsoap images and records their
	digest references in the root .env. With no service flags, no container is
	recreated. --liquidsoap refuses stopped channels and verifies that the
	composer container ID did not change.
	EOF
	exit 2
}

[[ $# -ge 1 ]] || usage
[[ "$1" == -h || "$1" == --help ]] && usage
RELEASE_FILE="$1"
shift

DEPLOY_BACKEND=0
CHANNELS=()
while [[ $# -gt 0 ]]; do
	case "$1" in
		--backend) DEPLOY_BACKEND=1 ;;
		--liquidsoap)
			shift
			[[ $# -gt 0 ]] || usage
			CHANNELS+=("$1")
			;;
		-h|--help) usage ;;
		*) die "unknown argument: $1" ;;
	esac
	shift
done

[[ -f "$RELEASE_FILE" ]] || die "release manifest not found: $RELEASE_FILE"
ENV_FILE="$REPO_ROOT/.env"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — run scripts/install.sh first"
grep -q $'\r' "$ENV_FILE" && die "$ENV_FILE has CRLF line endings — run scripts/install.sh to repair it"

release_value() {
	local key="$1" count value
	count="$(grep -c "^${key}=" "$RELEASE_FILE" || true)"
	[[ "$count" == 1 ]] || die "$RELEASE_FILE must contain exactly one ${key}= line"
	value="$(sed -n "s/^${key}=//p" "$RELEASE_FILE")"
	[[ -n "$value" ]] || die "$key is empty in $RELEASE_FILE"
	printf '%s' "$value"
}

TAG="$(release_value AMBIENT_RELEASE_TAG)"
COMMIT="$(release_value AMBIENT_RELEASE_COMMIT)"
RELEASE_REPOSITORY="$(release_value AMBIENT_RELEASE_REPOSITORY)"
BACKEND_IMAGE="$(release_value AMBIENT_BACKEND_IMAGE)"
LIQUIDSOAP_IMAGE="$(release_value AMBIENT_LIQUIDSOAP_IMAGE)"

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] \
	|| die "invalid release tag: $TAG"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "invalid release commit: $COMMIT"
[[ "$RELEASE_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/ambient-streamer$ ]] \
	|| die "invalid release repository: $RELEASE_REPOSITORY"
RELEASE_OWNER="${RELEASE_REPOSITORY%%/*}"
RELEASE_OWNER="${RELEASE_OWNER,,}"
[[ "$BACKEND_IMAGE" =~ ^ghcr\.io/${RELEASE_OWNER}/ambient-streamer-backend@sha256:[0-9a-f]{64}$ ]] \
	|| die "invalid backend image digest reference: $BACKEND_IMAGE"
[[ "$LIQUIDSOAP_IMAGE" =~ ^ghcr\.io/${RELEASE_OWNER}/ambient-streamer-liquidsoap@sha256:[0-9a-f]{64}$ ]] \
	|| die "invalid Liquidsoap image digest reference: $LIQUIDSOAP_IMAGE"

HEAD_COMMIT="$(git rev-parse HEAD)"
[[ "$HEAD_COMMIT" == "$COMMIT" ]] \
	|| die "checkout is $HEAD_COMMIT, release $TAG requires $COMMIT"

command -v docker >/dev/null 2>&1 || die "docker not found"
docker info >/dev/null 2>&1 || die "Docker daemon did not answer"

declare -A COMPOSER_IDS=()
for channel in "${CHANNELS[@]}"; do
	[[ "$channel" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "invalid channel name: $channel"
	[[ -z "${COMPOSER_IDS[$channel]+set}" ]] || die "duplicate channel: $channel"
	liquidsoap="${channel}-liquidsoap"
	composer="${channel}-composer"
	[[ "$(docker inspect -f '{{.State.Running}}' "$liquidsoap" 2>/dev/null || true)" == true ]] \
		|| die "$liquidsoap is not running — refusing to start a stopped channel"
	[[ "$(docker inspect -f '{{.State.Running}}' "$composer" 2>/dev/null || true)" == true ]] \
		|| die "$composer is not running — refusing a partial channel rollout"
	COMPOSER_IDS[$channel]="$(docker inspect -f '{{.Id}}' "$composer")"
done

if (( ${#CHANNELS[@]} > 0 )); then
	[[ "$(docker inspect -f '{{.State.Running}}' ambient-backend 2>/dev/null || true)" == true ]] \
		|| die "ambient-backend is not running — deploy the backend before Liquidsoap"
fi

log "pulling $TAG images"
docker pull "$BACKEND_IMAGE"
docker pull "$LIQUIDSOAP_IMAGE"

verify_image_identity() {
	local image="$1" name="$2" revision source
	revision="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"
	[[ "$revision" == "$COMMIT" ]] \
		|| die "$name image revision is $revision, expected $COMMIT"
	source="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.source" }}' "$image")"
	[[ "$source" == "https://github.com/$RELEASE_REPOSITORY" ]] \
		|| die "$name image source is $source, expected https://github.com/$RELEASE_REPOSITORY"
}

log "verifying image source and revision labels"
verify_image_identity "$BACKEND_IMAGE" backend
verify_image_identity "$LIQUIDSOAP_IMAGE" Liquidsoap
ok "both image identities match $RELEASE_REPOSITORY@$COMMIT"

# Process environment has higher Compose precedence than root/channel env files.
# Export the already-validated digests so neither a shell variable nor a
# channel-local .env can substitute another image during rollout.
export AMBIENT_BACKEND_IMAGE="$BACKEND_IMAGE"
export AMBIENT_LIQUIDSOAP_IMAGE="$LIQUIDSOAP_IMAGE"

umask 077
TMP_ENV="$(mktemp "${ENV_FILE}.release.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT
awk -v backend="$BACKEND_IMAGE" -v liquidsoap="$LIQUIDSOAP_IMAGE" '
	BEGIN { have_backend = 0; have_liquidsoap = 0 }
	/^AMBIENT_BACKEND_IMAGE=/ {
		if (!have_backend) print "AMBIENT_BACKEND_IMAGE=" backend
		have_backend = 1
		next
	}
	/^AMBIENT_LIQUIDSOAP_IMAGE=/ {
		if (!have_liquidsoap) print "AMBIENT_LIQUIDSOAP_IMAGE=" liquidsoap
		have_liquidsoap = 1
		next
	}
	{ print }
	END {
		if (!have_backend) print "AMBIENT_BACKEND_IMAGE=" backend
		if (!have_liquidsoap) print "AMBIENT_LIQUIDSOAP_IMAGE=" liquidsoap
	}
' "$ENV_FILE" > "$TMP_ENV"
chmod --reference="$ENV_FILE" "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"
trap - EXIT
ok "recorded immutable image references in .env"

if (( DEPLOY_BACKEND )); then
	docker compose --project-name ambient --env-file "$ENV_FILE" config --images \
		| grep -Fx "$BACKEND_IMAGE" >/dev/null \
		|| die "global Compose does not resolve backend to $BACKEND_IMAGE"
	log "recreating backend only"
	docker compose --project-name ambient --env-file "$ENV_FILE" \
		up -d --no-deps --force-recreate --wait --wait-timeout 60 backend
	actual_backend="$(docker inspect -f '{{.Config.Image}}' ambient-backend)"
	[[ "$actual_backend" == "$BACKEND_IMAGE" ]] \
		|| die "backend uses $actual_backend, expected $BACKEND_IMAGE"
	[[ "$(docker inspect -f '{{.State.Running}}' ambient-backend)" == true ]] \
		|| die "backend is not running after recreation"
	ok "backend uses $actual_backend"
fi

for channel in "${CHANNELS[@]}"; do
	liquidsoap="${channel}-liquidsoap"
	composer="${channel}-composer"
	before_id="${COMPOSER_IDS[$channel]}"

	log "rendering $channel compose file with the release image override"
	docker exec ambient-backend python -m ambient.compile "$channel"
	"$REPO_ROOT/scripts/channel.sh" config "$channel" --images \
		| grep -Fx "$LIQUIDSOAP_IMAGE" >/dev/null \
		|| die "channel Compose does not resolve $liquidsoap to $LIQUIDSOAP_IMAGE"
	"$REPO_ROOT/scripts/channel.sh" start "$channel" \
		--no-deps --force-recreate "$liquidsoap"

	actual_liquidsoap="$(docker inspect -f '{{.Config.Image}}' "$liquidsoap")"
	[[ "$actual_liquidsoap" == "$LIQUIDSOAP_IMAGE" ]] \
		|| die "$liquidsoap uses $actual_liquidsoap, expected $LIQUIDSOAP_IMAGE"
	[[ "$(docker inspect -f '{{.State.Running}}' "$liquidsoap")" == true ]] \
		|| die "$liquidsoap is not running after recreation"
	after_id="$(docker inspect -f '{{.Id}}' "$composer")"
	[[ "$before_id" == "$after_id" ]] \
		|| die "$composer changed during Liquidsoap rollout"
	ok "$liquidsoap uses the release digest; composer ID unchanged"
done

if (( ! DEPLOY_BACKEND )) && (( ${#CHANNELS[@]} == 0 )); then
	warn "images are prepared; no containers were recreated"
fi