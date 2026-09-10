#!/usr/bin/env bash
# ambient-streamer — scaffold one channel, end to end.
#
#   scripts/new-channel.sh lofi
#
# Creates channels/lofi/{audio,images,bumpers,profiles}, a 0600 .env and a
# config.yaml carrying the channel's own name and its own Icecast mounts, then
# encodes the channel's fallback MP3.
#
# It never prompts for a YouTube stream key and never writes one. Keys are
# created by hand in YouTube Studio and pasted into channels/lofi/.env; this
# script prints exactly where.
#
# Re-running against a channel that already exists changes nothing and exits
# non-zero — that .env may hold a stream key.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -t 2 ]]; then
	C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_OFF=$'\033[0m'
else
	C_RED=''; C_GRN=''; C_YLW=''; C_DIM=''; C_BLD=''; C_OFF=''
fi
log()   { printf '%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*" >&2; }
ok()    { printf '%s ok %s %s\n' "$C_GRN" "$C_OFF" "$*" >&2; }
warn()  { printf '%swarn%s %s\n' "$C_YLW" "$C_OFF" "$*" >&2; }
die()   { printf '%sfail%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
head1() { printf '\n%s%s%s\n' "$C_BLD" "$*" "$C_OFF" >&2; }

[[ $# -eq 1 ]] || die "usage: scripts/new-channel.sh <name>"
case "$1" in
	-h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 0 ;;
esac
CHANNEL="$1"

# The name becomes a directory, a Compose project, two container names and an
# Icecast mount, so it is the intersection of every one of those rules: no
# underscore (scripts/channel.sh rejects it) and no trailing hyphen (the
# backend rejects it).
[[ "$CHANNEL" =~ ^[a-z0-9]([a-z0-9-]{0,29}[a-z0-9])?$ ]] \
	|| die "'$CHANNEL' is not a channel name. Lower-case letters, digits and
       hyphens, starting and ending alphanumeric, 31 characters at most."
[[ "$CHANNEL" == "example" ]] \
	&& die "'example' is the template channel, not a stream. Pick another name."

TEMPLATE_DIR="$REPO_ROOT/channels/example"
TEMPLATE_ENV="$TEMPLATE_DIR/.env.example"
TEMPLATE_CFG="$TEMPLATE_DIR/config.yaml"
CHANNEL_DIR="$REPO_ROOT/channels/$CHANNEL"
CHANNEL_ENV="$CHANNEL_DIR/.env"
CHANNEL_CFG="$CHANNEL_DIR/config.yaml"
MOUNT="/$CHANNEL"
FALLBACK_MOUNT="/$CHANNEL-fallback"

[[ -f "$TEMPLATE_ENV" ]] || die "missing $TEMPLATE_ENV — the template channel is the source for this"
[[ -f "$TEMPLATE_CFG" ]] || die "missing $TEMPLATE_CFG"

# ---------------------------------------------------------------------------
# 1. Refuse to clobber
# ---------------------------------------------------------------------------
head1 "1/5  checks"

for existing in "$CHANNEL_ENV" "$CHANNEL_CFG"; do
	[[ -e "$existing" ]] && die "$(realpath --relative-to="$REPO_ROOT" "$existing") already exists.
       Refusing to overwrite it — that file may hold this channel's stream key.
       Delete the channel directory by hand if you really want to start over."
done
if [[ -d "$CHANNEL_DIR" ]]; then
	warn "channels/$CHANNEL exists but has no .env or config.yaml — filling it in"
fi

# Two channels on one Icecast mount fight over it: the second source is
# refused, and the first channel's audio is what the second one broadcasts.
shopt -s nullglob
for other_env in "$REPO_ROOT"/channels/*/.env; do
	other="$(basename "$(dirname "$other_env")")"
	[[ "$other" == "$CHANNEL" ]] && continue
	while IFS= read -r taken; do
		[[ -z "$taken" ]] && continue
		[[ "$taken" == "$MOUNT" || "$taken" == "$FALLBACK_MOUNT" ]] \
			&& die "mount $taken is already used by channel '$other'.
       Mounts must be unique across every channel — pick a different name."
	done < <(sed -n -E 's/^[[:space:]]*CHANNEL_(FALLBACK_)?MOUNT=(.*)$/\2/p' "$other_env" | tr -d '\r')
done
shopt -u nullglob
ok "$MOUNT and $FALLBACK_MOUNT are free"

# ---------------------------------------------------------------------------
# 2. Directories
# ---------------------------------------------------------------------------
head1 "2/5  directories"

# profiles/ matches what POST /api/channels creates; color extraction writes
# there. Its contents are generated, so it gets no .gitkeep.
for sub in audio images bumpers; do
	mkdir -p "$CHANNEL_DIR/$sub"
	: > "$CHANNEL_DIR/$sub/.gitkeep"
done
mkdir -p "$CHANNEL_DIR/profiles"
ok "channels/$CHANNEL/{audio,images,bumpers,profiles}"

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------
head1 "3/5  secrets file"

# Copied to .env, NOT to .env.example. A copy of the template left inside a
# real channel directory looks like the file to edit and is not one.
tmp="$(mktemp "$CHANNEL_DIR/.env.XXXXXX")"
chmod 600 "$tmp"
# tr, not cp: a CRLF .env makes compose interpolate a trailing \r into every
# value, including the stream key.
tr -d '\r' <"$TEMPLATE_ENV" | awk -v mount="$MOUNT" -v fallback="$FALLBACK_MOUNT" '
	/^CHANNEL_MOUNT=/          { print "CHANNEL_MOUNT=" mount; next }
	/^CHANNEL_FALLBACK_MOUNT=/ { print "CHANNEL_FALLBACK_MOUNT=" fallback; next }
	{ print }
' >"$tmp"

grep -qE '^CHANNEL_MOUNT='"$MOUNT"'$' "$tmp" \
	|| die "the template has no CHANNEL_MOUNT line — $TEMPLATE_ENV has drifted"
grep -qE '^CHANNEL_FALLBACK_MOUNT='"$FALLBACK_MOUNT"'$' "$tmp" \
	|| die "the template has no CHANNEL_FALLBACK_MOUNT line — $TEMPLATE_ENV has drifted"

# The template must never carry a real key; if it ever does, stop rather than
# copy one credential into every channel on the host.
if grep -qE '^YOUTUBE_STREAM_KEY=[^[:space:]]' "$tmp"; then
	die "$TEMPLATE_ENV contains a YOUTUBE_STREAM_KEY value. Clear it — a template
       with a key in it hands that key to every channel created from it."
fi

mv -f "$tmp" "$CHANNEL_ENV"
chmod 600 "$CHANNEL_ENV"
ok "channels/$CHANNEL/.env (mode 600) — mount $MOUNT, fallback $FALLBACK_MOUNT"

# ---------------------------------------------------------------------------
# 4. config.yaml
# ---------------------------------------------------------------------------
head1 "4/5  channel config"

tmp="$(mktemp "$CHANNEL_DIR/config.yaml.XXXXXX")"
chmod 644 "$tmp"
tr -d '\r' <"$TEMPLATE_CFG" | awk -v name="$CHANNEL" '
	!renamed && /^name:[[:space:]]/ { print "name: " name; renamed = 1; next }
	{ print }
' >"$tmp"
grep -qx "name: $CHANNEL" "$tmp" \
	|| die "could not set 'name:' in config.yaml — $TEMPLATE_CFG has drifted"
mv -f "$tmp" "$CHANNEL_CFG"
ok "channels/$CHANNEL/config.yaml — name: $CHANNEL"

# ---------------------------------------------------------------------------
# 5. Icecast fallback
# ---------------------------------------------------------------------------
head1 "5/5  icecast fallback"

# What the compositor hears while Liquidsoap restarts. Missing it is survivable
# — the entrypoint falls back to default.mp3 — but the per-channel file is what
# keeps a restart from reaching YouTube at all.
FALLBACK_DIR="${AMBIENT_FALLBACK_DIR:-$REPO_ROOT/common/fallback}"
if [[ -f "$FALLBACK_DIR/$CHANNEL.mp3" ]]; then
	ok "common/fallback/$CHANNEL.mp3 exists — left untouched"
elif ! command -v ffmpeg >/dev/null 2>&1; then
	warn "ffmpeg not on PATH — no fallback encoded. Run scripts/install.sh once it is."
else
	# </dev/null as well as make-fallback.sh's own -nostdin: an ffmpeg that
	# inherits stdin silently eats lines from any heredoc calling this script.
	"$REPO_ROOT/scripts/make-fallback.sh" "$CHANNEL" </dev/null >/dev/null \
		|| die "fallback generation failed for '$CHANNEL'"
	ok "common/fallback/$CHANNEL.mp3 (mp3 256k / 44.1k / stereo)"
fi

# Registering the mount is what gives Icecast a <fallback-mount> for this
# channel. Liquidsoap connects either way, so a missing entry has no visible
# symptom until a Liquidsoap restart takes the compositor down with it.
MOUNTS_LIST="${AMBIENT_DATA_DIR:-$REPO_ROOT/channels}/mounts.list"
if [[ -f "$MOUNTS_LIST" ]] && grep -qx "$CHANNEL" "$MOUNTS_LIST"; then
	ok "channels/mounts.list already lists $CHANNEL"
else
	[[ -f "$MOUNTS_LIST" ]] || printf '# One channel mount per line.\n' >"$MOUNTS_LIST"
	printf '%s\n' "$CHANNEL" >>"$MOUNTS_LIST"
	ok "channels/mounts.list += $CHANNEL"
	if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ambient-icecast; then
		docker kill -s HUP ambient-icecast >/dev/null 2>&1 \
			&& ok "icecast reloaded (SIGHUP, no restart)" \
			|| warn "could not SIGHUP ambient-icecast — reload it before starting the channel"
	else
		log "icecast not running; it will pick this up at start"
	fi
fi

# ---------------------------------------------------------------------------
head1 "next"

cat >&2 <<EOF

  ${C_BLD}1. Paste this channel's YouTube stream key${C_OFF}
     YouTube Studio -> Go Live -> Stream -> Select stream key (use a REUSABLE
     key, so a restart resumes the same broadcast). Then edit:

       channels/$CHANNEL/.env        ->  YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx-xxxx

     Each channel needs its own key on its own YouTube channel. That file is
     mode 600 and gitignored; nothing here reads, prints or logs the key, and
     no composer container is ever given one.

  2. Add media
       cp /path/to/*.mp3  channels/$CHANNEL/audio/
       cp /path/to/*.jpg  channels/$CHANNEL/images/
     Anything shared by several channels belongs in common/ instead.

  3. Check the host has room for it
       scripts/capacity-check.sh
     Every plugin in hot_set renders on every frame, whether or not it is the
     one on air, so the hot set is the main thing that decides whether a channel
     holds realtime.

  4. Compile and start
       docker exec ambient-backend python -m ambient.compile $CHANNEL
       scripts/channel.sh start $CHANNEL

EOF

ok "channel '$CHANNEL' scaffolded"
