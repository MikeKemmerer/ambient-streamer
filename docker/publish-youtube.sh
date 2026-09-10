#!/bin/sh
# ambient-streamer — MediaMTX runOnReady hook: publish one channel to YouTube.
#
# MediaMTX starts this as `sh -c "exec <this>"` with MTX_PATH set when a
# channel's program path becomes ready, and kills it when the path stops being
# ready. That is the supervision: FFmpeg's -reconnect* flags do NOT apply to
# the RTMP demuxer, so a bare publisher dies permanently the first time the
# composer restarts (spike S5, variants A and C). Restarts come from
# runOnReadyRestart in docker/mediamtx.yml, never from FFmpeg.
#
# The stream key is read from channels/<name>/.env, which is bind-mounted here
# read-only. It reaches FFmpeg through a 0600 tmpfs file and the preloaded
# argv shim, so it never appears in this container's command, in `ps`, in
# `docker inspect`, or in a log line.
set -eu

CHANNEL="${MTX_PATH:-}"
CHANNELS_DIR="${AMBIENT_CHANNELS_DIR:-/etc/ambient/channels}"
RUN_DIR="${AMBIENT_PUBLISH_RUN_DIR:-/run/ambient/publish}"
RTMP_PORT="${AMBIENT_RTMP_PORT:-1935}"
RECHECK="${AMBIENT_PUBLISH_RECHECK:-60}"
SHIM="${AMBIENT_ARGV_SHIM:-/usr/local/lib/libambientargv.so}"
LOGLEVEL="${AMBIENT_PUBLISH_LOGLEVEL:-warning}"
# Both are CAPS, not waits: FFmpeg returns as soon as it has the parameters. But it
# must outlast one GOP, because -c copy without the SPS/PPS from an IDR produces a
# stream YouTube accepts and then drops ~15s later. At the composer's 2s GOP, 500ms
# (the S5 figure) recovered video parameters in only 1 of 3 attempts; 2.5s in 3 of 3.
ANALYZE="${AMBIENT_PUBLISH_ANALYZEDURATION:-3000000}"
PROBESIZE="${AMBIENT_PUBLISH_PROBESIZE:-4000000}"
TOKEN='@AMBIENT_PLAYPATH@'

log() { printf '%s INF [publish %s] %s\n' "$(date '+%Y/%m/%d %H:%M:%S')" "${CHANNEL:-?}" "$*"; }
die() { log "$*"; exit 1; }

# Stay resident instead of exiting: exiting would hand MediaMTX a restart loop.
# MediaMTX kills this when the path goes away, so nothing is left behind.
park() {
	log "$*"
	log "staying down; will re-check every ${RECHECK}s"
}

# Same expression as the program-feed path in docker/mediamtx.yml. MTX_PATH is
# attacker-influenced input as far as this script is concerned, and it is about
# to be used as a path component.
printf '%s' "$CHANNEL" | grep -qE '^[a-z0-9][a-z0-9-]{0,30}$' \
	|| die "refusing to publish: MTX_PATH is not a channel name"

ENV_FILE="$CHANNELS_DIR/$CHANNEL/.env"

# Read one value without sourcing the file — a .env is data, not code.
read_var() {
	[ -r "$ENV_FILE" ] || return 0
	sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
		| tail -n 1 \
		| tr -d '\r' \
		| sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Block until the channel has a usable key. An empty key is the normal state of
# a channel that is not meant to go live yet, so it must not hot-loop.
warned=0
while :; do
	if [ ! -r "$ENV_FILE" ]; then
		reason="no readable $ENV_FILE"
	else
		KEY="$(read_var YOUTUBE_STREAM_KEY)"
		[ -n "$KEY" ] && break
		reason="YOUTUBE_STREAM_KEY is empty in $ENV_FILE"
	fi
	[ "$warned" -eq 0 ] && park "$reason"
	warned=1
	sleep "$RECHECK"
done

RTMP_URL="$(read_var YOUTUBE_RTMP_URL)"
[ -n "$RTMP_URL" ] || RTMP_URL='rtmp://a.rtmp.youtube.com/live2'

# Split rtmp://host/app into the two halves FFmpeg wants as separate options.
# Keeping the app out of the URL is not the point; keeping the KEY out of it is
# — as -rtmp_playpath it stays out of every line FFmpeg logs.
RTMP_APP="${RTMP_URL##*/}"
RTMP_BASE="${RTMP_URL%/*}"
case "$RTMP_BASE" in
	rtmp://*|rtmps://*) ;;
	*) die "YOUTUBE_RTMP_URL is not an rtmp(s) ingest URL" ;;
esac
[ -n "$RTMP_APP" ] || die "YOUTUBE_RTMP_URL has no application component"

mkdir -p "$RUN_DIR"
chmod 0700 "$RUN_DIR"
PLAYPATH_FILE="$RUN_DIR/$CHANNEL.playpath"
( umask 077; printf '%s' "$KEY" >"$PLAYPATH_FILE" )
unset KEY

[ -r "$SHIM" ] || die "argv shim missing at $SHIM — refusing to put the key in argv"

log "publishing to ${RTMP_BASE}/${RTMP_APP} (key withheld from argv)"

exec env \
	LD_PRELOAD="$SHIM" \
	AMBIENT_PLAYPATH_FILE="$PLAYPATH_FILE" \
	ffmpeg -nostdin -hide_banner -loglevel "$LOGLEVEL" \
		-fflags nobuffer -analyzeduration "$ANALYZE" -probesize "$PROBESIZE" \
		-i "rtmp://127.0.0.1:${RTMP_PORT}/${CHANNEL}" \
		-c copy \
		-f flv -flvflags no_duration_filesize \
		-rtmp_app "$RTMP_APP" -rtmp_playpath "$TOKEN" \
		"$RTMP_BASE" </dev/null
