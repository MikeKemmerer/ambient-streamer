#!/usr/bin/env bash
# Generates Icecast fallback MP3s.
#
# The fallback file MUST be encoded identically to the live stream — MP3
# 256 kbps / 44.1 kHz / stereo. Icecast splices it into an already-open HTTP
# connection, so a different sample rate, channel count or codec is a format
# change mid-demux, which is the Ogg failure mode by another route.
# See docs/contracts/audio-transport.md.
#
#   scripts/make-fallback.sh                       -> common/fallback/default.mp3 (silence)
#   scripts/make-fallback.sh lofi                  -> common/fallback/lofi.mp3    (silence)
#   scripts/make-fallback.sh lofi path/to/bed.wav  -> common/fallback/lofi.mp3    (looped bed)
set -euo pipefail

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; NC=$'\033[0m'
log()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$NC" "$*" >&2; }
die()  { printf '%s✘%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${AMBIENT_FALLBACK_DIR:-$REPO_ROOT/common/fallback}"

# Not negotiable — these three values are the contract.
BITRATE=256k
SAMPLERATE=44100
CHANNELS=2
DURATION="${FALLBACK_SECONDS:-30}"

CHANNEL="${1:-default}"
SOURCE="${2:-}"
DEST="$OUT_DIR/${CHANNEL}.mp3"

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg not found on PATH"
mkdir -p "$OUT_DIR"

if [[ -n "$SOURCE" ]]; then
  [[ -f "$SOURCE" ]] || die "source not found: $SOURCE"
  log "encoding ${DURATION}s fallback for '$CHANNEL' from $SOURCE"
  ffmpeg -nostdin -hide_banner -loglevel error -y \
    -stream_loop -1 -i "$SOURCE" -t "$DURATION" \
    -af "aresample=${SAMPLERATE},afade=t=in:st=0:d=1,afade=t=out:st=$((DURATION - 1)):d=1" \
    -c:a libmp3lame -b:a "$BITRATE" -ar "$SAMPLERATE" -ac "$CHANNELS" \
    "$DEST"
else
  # Digital silence, not a tone: this plays on-air whenever Liquidsoap is down.
  log "encoding ${DURATION}s silent fallback for '$CHANNEL'"
  # -nostdin: without it ffmpeg swallows the caller's stdin, which silently eats
  # lines from any heredoc this script is invoked from.
  ffmpeg -nostdin -hide_banner -loglevel error -y \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=${SAMPLERATE}" -t "$DURATION" \
    -c:a libmp3lame -b:a "$BITRATE" -ar "$SAMPLERATE" -ac "$CHANNELS" \
    "$DEST"
fi

# Icecast serves this through fileserve; it must be world-readable inside the container.
chmod 644 "$DEST"

read -r fmt rate ch br < <(
  ffprobe -v error -select_streams a:0 \
    -show_entries stream=codec_name,sample_rate,channels,bit_rate \
    -of default=nw=1:nk=1 "$DEST" | paste -sd' '
)
[[ "$fmt" == "mp3" && "$rate" == "$SAMPLERATE" && "$ch" == "$CHANNELS" ]] \
  || die "encoded file is $fmt/${rate}Hz/${ch}ch, expected mp3/${SAMPLERATE}Hz/${CHANNELS}ch"

ok "$DEST — ${fmt} ${rate}Hz ${ch}ch ${br:-?}bps"
log "Icecast serves it as /fallback/${CHANNEL}.mp3"
