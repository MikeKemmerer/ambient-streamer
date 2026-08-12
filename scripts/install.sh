#!/usr/bin/env bash
# ambient-streamer — first-run setup.
#
# Idempotent by construction: every step checks for its own result first, and
# an existing secret is NEVER regenerated. Re-running this is the supported way
# to re-check a host after changing something.
#
#   scripts/install.sh
#   scripts/install.sh --skip-fallback     # don't encode the Icecast fallbacks
#   scripts/install.sh --check             # probe only; write nothing
#
# What it deliberately does NOT do: build images, start containers, or ask for
# a YouTube stream key. Stream keys are per channel, are created by hand in
# YouTube Studio, and live only in channels/<name>/.env.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -t 2 ]]; then
	C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
	C_RED=''; C_GRN=''; C_YLW=''; C_DIM=''; C_BLD=''; C_OFF=''
fi
log()  { printf '%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*" >&2; }
ok()   { printf '%s ok %s %s\n' "$C_GRN" "$C_OFF" "$*" >&2; }
warn() { printf '%swarn%s %s\n' "$C_YLW" "$C_OFF" "$*" >&2; }
die()  { printf '%sfail%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
head1() { printf '\n%s%s%s\n' "$C_BLD" "$*" "$C_OFF" >&2; }

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
YAML_FILE="$REPO_ROOT/ambient.yaml"
YAML_EXAMPLE="$REPO_ROOT/ambient.yaml.example"

CHECK_ONLY=0
SKIP_FALLBACK=0
WARNINGS=0

while [[ $# -gt 0 ]]; do
	case "$1" in
		--check)          CHECK_ONLY=1 ;;
		--skip-fallback)  SKIP_FALLBACK=1 ;;
		-h|--help)
			sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
			exit 0 ;;
		*) die "unknown argument: $1 (try --help)" ;;
	esac
	shift
done

note_warning() { WARNINGS=$((WARNINGS + 1)); warn "$@"; }

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
head1 "1/7  prerequisites"

need() {
	command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH. $2"
}

need docker   "Install Docker Engine: https://docs.docker.com/engine/install/"
need ffmpeg   "Install ffmpeg — the fallback encoder and the encoder probe both need it."
need ffprobe  "ffprobe ships with ffmpeg; a partial install is worse than none."
need openssl  "Install openssl — it generates the Icecast and API secrets."

docker compose version >/dev/null 2>&1 \
	|| die "the 'docker compose' plugin is missing. Install docker-compose-plugin;
       the standalone docker-compose v1 binary is not supported."

docker info >/dev/null 2>&1 \
	|| die "the Docker daemon did not answer. Start it, or add $(id -un) to the
       'docker' group and open a new session."

ok "docker $(docker version --format '{{.Server.Version}}')"
ok "docker compose $(docker compose version --short)"
ok "$(ffmpeg -version | head -1 | cut -d' ' -f1-3)"

DOCKER_SOCK=/var/run/docker.sock
if [[ -S "$DOCKER_SOCK" ]]; then
	SOCK_GID="$(stat -c '%g' "$DOCKER_SOCK")"
	if [[ "$SOCK_GID" == "0" ]]; then
		note_warning "$DOCKER_SOCK is group root. The backend container drops to PUID/PGID
       and will refuse to start; give the socket a non-root group, or run the
       backend as root and accept that the API handler shares the socket's uid."
	else
		ok "docker socket group gid $SOCK_GID — the backend can drop privileges"
	fi
else
	note_warning "$DOCKER_SOCK not found. The backend needs it to drive 'docker compose'."
fi

# ---------------------------------------------------------------------------
# 2. Config files
# ---------------------------------------------------------------------------
head1 "2/7  configuration"

seed_from_example() {
	local target="$1" example="$2" mode="$3"
	if [[ -f "$target" ]]; then
		ok "$(basename "$target") exists — left untouched"
		return
	fi
	[[ -f "$example" ]] || die "$example is missing; cannot create $(basename "$target")"
	if (( CHECK_ONLY )); then
		note_warning "$(basename "$target") is missing (--check: not created)"
		return
	fi
	# tr, not cp: a CRLF .env makes every value end in \r, which compose
	# interpolates verbatim into a password.
	tr -d '\r' <"$example" >"$target"
	chmod "$mode" "$target"
	ok "created $(basename "$target") from $(basename "$example") (mode $mode)"
}

seed_from_example "$ENV_FILE"  "$ENV_EXAMPLE"  600
seed_from_example "$YAML_FILE" "$YAML_EXAMPLE" 644

[[ -f "$ENV_FILE" ]] || die ".env is required past this point; re-run without --check"

# An .env edited on Windows breaks in a way that is very hard to see: the
# secret checks below would find ICECAST_SOURCE_PASSWORD=\r non-empty and skip
# generation, and compose would carry the \r into the password itself.
if grep -q $'\r' "$ENV_FILE"; then
	if (( CHECK_ONLY )); then
		note_warning ".env has CRLF line endings (--check: not repaired)"
	else
		tr -d '\r' <"$ENV_FILE" >"${ENV_FILE}.tmp" && mv -f "${ENV_FILE}.tmp" "$ENV_FILE"
		chmod 600 "$ENV_FILE"
		warn ".env had CRLF line endings — stripped. Every value carried a trailing \\r."
	fi
fi

# .env holds the Icecast credentials and the API token.
if (( ! CHECK_ONLY )); then
	chmod 600 "$ENV_FILE"
fi
current_mode="$(stat -c '%a' "$ENV_FILE")"
[[ "$current_mode" == "600" ]] && ok ".env is mode 600" || note_warning ".env is mode $current_mode, expected 600"

# ---------------------------------------------------------------------------
# 3. Secrets — generated only where empty, never regenerated, never printed
# ---------------------------------------------------------------------------
head1 "3/7  secrets"

# True when KEY exists in .env with a non-empty value. Reads the value but
# never emits it.
env_is_set() {
	awk -F= -v key="$1" '
		{ sub(/\r$/, "") }
		$1 == key && length($0) > length(key) + 1 { found = 1 }
		END { exit !found }
	' "$ENV_FILE"
}

env_value() {
	sed -n -E "s/^$1=(.*)$/\1/p" "$ENV_FILE" | tr -d '\r' | tail -n1
}

# The value travels through the environment, not argv, so it is never visible
# in `ps` — the same rule docker/publish-youtube.sh follows for stream keys.
env_put() {
	local key="$1" tmp
	tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
	chmod 600 "$tmp"
	if grep -qE "^${key}=" "$ENV_FILE"; then
		AMBIENT_PUT_VALUE="$2" awk -v key="$key" '
			$0 ~ "^" key "=" && !done { printf "%s=%s\n", key, ENVIRON["AMBIENT_PUT_VALUE"]; done = 1; next }
			{ print }
		' "$ENV_FILE" >"$tmp"
	else
		cp "$ENV_FILE" "$tmp"
		printf '%s=%s\n' "$key" "$2" >>"$tmp"
	fi
	mv -f "$tmp" "$ENV_FILE"
	chmod 600 "$ENV_FILE"
}

ensure_secret() {
	local key="$1" bytes="$2"
	if env_is_set "$key"; then
		ok "$key already set — not regenerated"
		return
	fi
	if (( CHECK_ONLY )); then
		note_warning "$key is empty (--check: not generated)"
		return
	fi
	# Assigned and consumed in one statement; never echoed, never logged.
	env_put "$key" "$(openssl rand -hex "$bytes")"
	ok "$key generated (${bytes} bytes)"
}

# Rotating any of these means recreating the containers that hold them, so the
# "never overwrite" rule above is what makes this script safe to re-run on a
# live install.
ensure_secret ICECAST_SOURCE_PASSWORD 16
ensure_secret ICECAST_ADMIN_PASSWORD  16
ensure_secret ICECAST_RELAY_PASSWORD  16
ensure_secret AMBIENT_API_TOKEN       32

# Not a secret, but the backend is unusable without it: the daemon resolves a
# bind source on the host, so the backend must see the repo at its host path.
recorded_root="$(env_value AMBIENT_REPO_ROOT)"
if [[ "$recorded_root" == "$REPO_ROOT" ]]; then
	ok "AMBIENT_REPO_ROOT=$REPO_ROOT"
elif (( CHECK_ONLY )); then
	note_warning "AMBIENT_REPO_ROOT is '${recorded_root:-unset}', expected $REPO_ROOT"
else
	env_put AMBIENT_REPO_ROOT "$REPO_ROOT"
	if [[ -n "$recorded_root" ]]; then
		warn "AMBIENT_REPO_ROOT was '$recorded_root' — updated to $REPO_ROOT"
		warn "recreate the backend so it remounts: docker compose -p ambient up -d backend"
	else
		ok "AMBIENT_REPO_ROOT set to $REPO_ROOT"
	fi
fi

shopt -s nullglob
for channel_env in "$REPO_ROOT"/channels/*/.env; do
	if (( ! CHECK_ONLY )); then chmod 600 "$channel_env"; fi
	ok "$(realpath --relative-to="$REPO_ROOT" "$channel_env") is mode $(stat -c '%a' "$channel_env")"
done
shopt -u nullglob

# ---------------------------------------------------------------------------
# 4. Paths
# ---------------------------------------------------------------------------
head1 "4/7  paths"

resolve_path() {
	local value="$1" fallback="$2"
	[[ -n "$value" ]] || value="$fallback"
	[[ "$value" == /* ]] && { printf '%s\n' "${value%/}"; return; }
	printf '%s\n' "$REPO_ROOT/${value#./}"
}

DATA_DIR="$(resolve_path "$(env_value AMBIENT_DATA_DIR)" ./channels)"
COMMON_DIR="$(resolve_path "$(env_value AMBIENT_COMMON_DIR)" ./common)"
LOG_DIR="$(env_value AMBIENT_LOG_DIR)"
: "${LOG_DIR:=/var/log/ambient}"

[[ "$LOG_DIR" == /* ]] \
	|| die "AMBIENT_LOG_DIR must be absolute (got '$LOG_DIR'). It is bind-mounted
       into the backend at the same path on both sides."

for dir in "$DATA_DIR" "$COMMON_DIR/audio" "$COMMON_DIR/images" "$COMMON_DIR/bumpers" \
           "$COMMON_DIR/fallback" "$COMMON_DIR/profiles"; do
	if [[ -d "$dir" ]]; then continue; fi
	if (( CHECK_ONLY )); then note_warning "$dir is missing (--check)"; continue; fi
	mkdir -p "$dir"
	log "created $dir"
done
ok "channel data: $DATA_DIR"
ok "shared media: $COMMON_DIR"

if [[ -d "$LOG_DIR" ]]; then
	ok "logs: $LOG_DIR"
elif (( CHECK_ONLY )) || ! mkdir -p "$LOG_DIR" 2>/dev/null; then
	note_warning "cannot create $LOG_DIR. Run:
       sudo install -d -o $(id -u) -g $(id -g) -m 755 '$LOG_DIR'"
else
	ok "logs: $LOG_DIR (created)"
fi

# 9p and network filesystems cannot sustain a 24/7 read pattern. This is the
# single most common way a first install ends up stuttering.
check_filesystem() {
	local dir="$1" label="$2" fstype
	[[ -d "$dir" ]] || return 0
	fstype="$(stat -f -c '%T' "$dir" 2>/dev/null || echo unknown)"
	case "$dir" in
		/mnt/c/*|/mnt/[a-z]/*)
			note_warning "$label is on $dir — a Windows drive through 9p. Far too slow for
       24/7 reads. Move it to a WSL2 ext4 path or a Docker named volume."
			return ;;
	esac
	case "$fstype" in
		nfs*|cifs|smb*|fuseblk|9p|v9fs)
			note_warning "$label is on a $fstype filesystem ($dir). A network round trip per
       read will stall a 24/7 stream; use local storage." ;;
		*) ok "$label on $fstype" ;;
	esac
}
check_filesystem "$DATA_DIR"   "channel data"
check_filesystem "$COMMON_DIR" "shared media"

# ---------------------------------------------------------------------------
# 5. Port conflicts — before anything binds, not after
# ---------------------------------------------------------------------------
head1 "5/7  ports"

BIND_ADDRESS="$(env_value AMBIENT_BIND_ADDRESS)"
: "${BIND_ADDRESS:=127.0.0.1}"
BACKEND_PORT="$(env_value AMBIENT_BACKEND_PORT)"
: "${BACKEND_PORT:=8090}"
HLS_PUBLISH="$(env_value AMBIENT_HLS_PUBLISH)"

# Which container publishes this port, if any. Named so the message can say
# "held by dizquetv" instead of "address already in use".
docker_holder() {
	docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null \
		| awk -F'\t' -v port="$1" '$2 ~ (":" port "->") { print $1 }' \
		| paste -sd, -
}

# ss shows the process only for sockets this user owns; say so rather than
# reporting an empty command as if nothing held the port.
socket_holder() {
	local port="$1"
	command -v ss >/dev/null 2>&1 || { printf 'unknown (ss not installed)\n'; return; }
	ss -ltnp 2>/dev/null | awk -v port=":$port" '
		NR == 1 { next }
		{
			addr = $4
			if (index(addr, port) && substr(addr, length(addr) - length(port) + 1) == port) {
				proc = $NF
				gsub(/users:\(\(|\)\)|"/, "", proc)
				printf "%s on %s\n", (proc == "" || proc ~ /^[0-9]/ ? "another user'\''s process (re-run with sudo to name it)" : proc), addr
			}
		}' | sort -u | paste -sd'; ' -
}

port_free() {
	local port="$1" label="$2" holder detail
	holder="$(docker_holder "$port")"
	detail="$(socket_holder "$port")"
	if [[ -z "$holder" && -z "$detail" ]]; then
		ok "$port free — $label"
		return 0
	fi
	# Our own stack re-running is not a conflict.
	if [[ -n "$holder" && "$holder" =~ ^ambient- ]]; then
		ok "$port held by $holder — this stack, already running"
		return 0
	fi
	printf '%sfail%s %s\n' "$C_RED" "$C_OFF" "port $port ($label) is already in use:" >&2
	[[ -n "$holder" ]] && printf '         container: %s\n' "$holder" >&2
	[[ -n "$detail" ]] && printf '         listener:  %s\n' "$detail" >&2
	printf '         Free it, or change the port in .env, then re-run.\n' >&2
	return 1
}

PORT_CONFLICT=0
port_free "$BACKEND_PORT" "backend API + operator UI on $BIND_ADDRESS" || PORT_CONFLICT=1
if [[ -n "$HLS_PUBLISH" ]]; then
	port_free "$HLS_PUBLISH" "HLS preview (AMBIENT_HLS_PUBLISH is set)" || PORT_CONFLICT=1
fi

# Icecast 8081, RTMP 1935, HLS 8888 and the MediaMTX API 9997 are reachable
# only inside the compose network, so a host listener on them is not a
# conflict. Reported because publishing one later would make it one.
for pair in "$(env_value AMBIENT_ICECAST_PORT):icecast" \
            "$(env_value AMBIENT_RTMP_PORT):relay RTMP" \
            "$(env_value AMBIENT_HLS_PORT):relay HLS" \
            "9997:relay API"; do
	p="${pair%%:*}"; l="${pair#*:}"
	[[ -n "$p" ]] || continue
	if [[ -n "$(socket_holder "$p")" ]]; then
		log "$p ($l) busy on the host — internal to the compose network, so not a conflict"
	else
		log "$p ($l) internal only, host side free"
	fi
done

(( PORT_CONFLICT == 0 )) || die "refusing to continue with a port conflict. Nothing was started."

# ---------------------------------------------------------------------------
# 6. Encoders — a real encode, not `ffmpeg -encoders`
# ---------------------------------------------------------------------------
head1 "6/7  encoders"

# `ffmpeg -encoders` lists what the binary was compiled with, not what this
# machine can do. It advertises h264_qsv on hosts with no Intel device and
# h264_nvenc where the driver and the library disagree. The only reliable
# question is whether a frame comes out.
test_encode() {
	ffmpeg -hide_banner -nostdin -loglevel error \
		-f lavfi -i "color=c=black:s=320x240:r=10:d=0.4" \
		-c:v "$1" -f null - >/dev/null 2>&1
}

# Authoritative when the composer image exists: that is the ffmpeg that will
# actually run, with the device flags the channel will actually get.
test_encode_container() {
	local enc="$1"; shift
	docker run --rm --entrypoint ffmpeg "$@" "$COMPOSER_IMAGE" \
		-hide_banner -nostdin -loglevel error \
		-f lavfi -i "color=c=black:s=320x240:r=10:d=0.4" \
		-c:v "$enc" -f null - >/dev/null 2>&1
}

COMPOSER_IMAGE="${AMBIENT_COMPOSER_IMAGE:-ambient-composer:dev}"
HAVE_COMPOSER_IMAGE=0
docker image inspect "$COMPOSER_IMAGE" >/dev/null 2>&1 && HAVE_COMPOSER_IMAGE=1

USABLE=()

probe() {
	local enc="$1" host_note="$2"; shift 2
	local host_ok=1 ctr_result=""
	test_encode "$enc" || host_ok=0

	if (( HAVE_COMPOSER_IMAGE )); then
		if test_encode_container "$enc" "$@"; then ctr_result=pass; else ctr_result=fail; fi
	fi

	if [[ "$ctr_result" == "pass" ]] || { [[ -z "$ctr_result" ]] && (( host_ok )); }; then
		USABLE+=("$enc")
		ok "$enc — real encode succeeded${ctr_result:+ in $COMPOSER_IMAGE}"
		return
	fi
	if [[ "$ctr_result" == "fail" ]] && (( host_ok )); then
		note_warning "$enc — works on the host but NOT in $COMPOSER_IMAGE. The container is
       what runs; treat this encoder as unavailable. $host_note"
		return
	fi
	log "$enc unavailable — $host_note"
}

# QSV needs a real Intel render node. /dev/dri existing is not enough: on a
# host whose only render node belongs to an NVIDIA card, it is present and QSV
# still cannot initialise. The encode is the test.
QSV_NOTE="needs an Intel render node; /dev/dri is not exposed on Docker Desktop/WSL2 at all"
if [[ -d /dev/dri ]]; then
	log "/dev/dri present: $(ls /dev/dri | paste -sd' ' -) — presence proves nothing, probing"
	probe h264_qsv "$QSV_NOTE" --device /dev/dri:/dev/dri
else
	log "h264_qsv unavailable — no /dev/dri on this host ($QSV_NOTE)"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
	if ! nvidia-smi -L >/dev/null 2>&1; then
		note_warning "nvidia-smi is installed but failed: $(nvidia-smi -L 2>&1 | head -1)
       NVENC will be advertised by ffmpeg and fail at runtime."
	fi
	probe h264_nvenc "needs a working driver plus nvidia-container-toolkit" --gpus all
else
	log "h264_nvenc unavailable — no nvidia-smi on this host"
fi

probe libx264 "the software fallback should always work; something is wrong"

[[ ${#USABLE[@]} -gt 0 ]] || die "no usable video encoder. Even libx264 failed — check the ffmpeg install."
ok "usable encoders: ${USABLE[*]}"

DEFAULT_ENCODER="$(env_value AMBIENT_DEFAULT_ENCODER)"
: "${DEFAULT_ENCODER:=libx264}"
if [[ " ${USABLE[*]} " == *" $DEFAULT_ENCODER "* ]]; then
	ok "AMBIENT_DEFAULT_ENCODER=$DEFAULT_ENCODER is usable here"
else
	note_warning "AMBIENT_DEFAULT_ENCODER=$DEFAULT_ENCODER did not pass its encode test.
       Channels will need an override, or set it to one of: ${USABLE[*]}"
fi

(( HAVE_COMPOSER_IMAGE )) || log "$COMPOSER_IMAGE not built yet — probed with the host ffmpeg.
    Re-run this after 'docker compose -p ambient build' for the authoritative answer."

# Capacity, from the measured cost: ~1.5 cores for one 720p channel with one
# hot plugin. Higher than filter benchmarks suggest because the HLS preview is
# a second encode, not a free tap off the first.
CORES="$(nproc)"
RESERVED="$(sed -n -E 's/^[[:space:]]*reserved_cores:[[:space:]]*([0-9.]+).*/\1/p' "$YAML_FILE" 2>/dev/null | head -1)"
: "${RESERVED:=1.0}"
CAPACITY="$(awk -v c="$CORES" -v r="$RESERVED" 'BEGIN { n = int((c - r) / 1.5); print (n < 0 ? 0 : n) }')"
ok "$CORES cores, $RESERVED reserved — room for about $CAPACITY concurrent 720p channels"
(( CAPACITY >= 1 )) || note_warning "this host cannot run even one channel within its own budget."

# ---------------------------------------------------------------------------
# 7. Icecast fallback
# ---------------------------------------------------------------------------
head1 "7/7  icecast fallback"

# The fallback is what the compositor hears while Liquidsoap is restarting. No
# fallback means the mount 404s and the compositor's input dies — which is the
# one failure the whole audio transport exists to prevent.
if (( SKIP_FALLBACK )); then
	log "skipped (--skip-fallback)"
elif (( CHECK_ONLY )); then
	[[ -f "$COMMON_DIR/fallback/default.mp3" ]] \
		&& ok "default.mp3 present" \
		|| note_warning "common/fallback/default.mp3 is missing (--check: not generated)"
else
	make_fallback() {
		local name="$1"
		if [[ -f "$COMMON_DIR/fallback/${name}.mp3" ]]; then
			ok "fallback/${name}.mp3 exists — left untouched"
			return
		fi
		AMBIENT_FALLBACK_DIR="$COMMON_DIR/fallback" "$REPO_ROOT/scripts/make-fallback.sh" "$name" >/dev/null \
			|| die "fallback generation failed for '$name'"
		ok "fallback/${name}.mp3 encoded (mp3 256k / 44.1k / stereo)"
	}
	make_fallback default
	shopt -s nullglob
	for channel_dir in "$DATA_DIR"/*/; do
		name="$(basename "$channel_dir")"
		[[ "$name" == "example" ]] && continue
		[[ -f "$channel_dir/.env" ]] || continue
		make_fallback "$name"
	done
	shopt -u nullglob
fi

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
head1 "done"

if (( WARNINGS > 0 )); then
	warn "$WARNINGS warning(s) above. None block a start, all are worth reading."
fi

cat >&2 <<EOF

  ${C_BLD}The backend holds the Docker socket, which is root-equivalent on this host.${C_OFF}
  It is published on ${BIND_ADDRESS}:${BACKEND_PORT} only, and every route but
  /api/health requires the bearer token in .env. Do not move it off loopback
  without putting real authentication in front of it.

  Next:

    1. Build the images
         docker compose -p ambient build

    2. Re-run this script — with the images built, the encoder probe becomes
       authoritative instead of a host approximation
         scripts/install.sh --check

    3. Start the global stack
         docker compose -p ambient up -d

    4. Prove the audio transport before adding a channel
         scripts/verify-stack.sh

    5. Add a channel. The YouTube stream key goes in channels/<name>/.env and
       nowhere else — never in .env, never in an image, never on a command line
         mkdir -p channels/<name>/{audio,images}
         cp channels/example/.env.example channels/<name>/.env
         chmod 600 channels/<name>/.env
         \$EDITOR channels/<name>/.env
         scripts/install.sh            # encodes that channel's fallback

    6. Open the UI
         http://${BIND_ADDRESS}:${BACKEND_PORT}/
       Read the token with:  grep '^AMBIENT_API_TOKEN=' .env

EOF

ok "install.sh complete"
