#!/usr/bin/env bash
# Compositor entrypoint: slideshow producer -> FFmpeg -> MediaMTX (full + preview).
#
# This process must run for the life of the channel. A restart is a new YouTube
# ingest session, so nothing here may depend on relaunching FFmpeg: images
# arrive on image2pipe, colors change over zmq, and visualization frames arrive
# through a framekeeper-owned rawvideo pipe.
#
# See docs/contracts/{audio-transport,slideshow,plugin,zmq-control}.md and the
# youtube-ingest skill. Encoder flags are verbatim from that skill.
set -euo pipefail
umask 077

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s composer %s\n' "$BLU" "$NC" "$*" >&2; }
ok()   { printf '%s[ok]%s composer %s\n' "$GRN" "$NC" "$*" >&2; }
warn() { printf '%s[!!]%s composer %s\n' "$YLW" "$NC" "$*" >&2; }
die()  { printf '%s[XX]%s composer %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- environment
CHANNEL_NAME="${CHANNEL_NAME:-}"
[[ -n "$CHANNEL_NAME" ]] || die "CHANNEL_NAME is required"
[[ "$CHANNEL_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] \
  || die "CHANNEL_NAME must contain only lowercase letters, digits and hyphens"

WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-30}"
PRODUCER_FPS="${PRODUCER_FPS:-10}"
for dimension in WIDTH HEIGHT FPS PRODUCER_FPS; do
  value="${!dimension}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$dimension must be a positive integer"
done
(( WIDTH % 2 == 0 && HEIGHT % 2 == 0 )) \
  || die "WIDTH and HEIGHT must be even for yuv420p"
LAYER_WIDTH=$(( WIDTH < 1280 ? WIDTH : 1280 ))
LAYER_HEIGHT=$(( HEIGHT < 720 ? HEIGHT : 720 ))
LAYER_FPS=$(( FPS < 30 ? FPS : 30 ))
ENCODER="${ENCODER:-libx264}"
PRESET="${X264_PRESET:-veryfast}"

ICECAST_HOST="${ICECAST_HOST:-icecast}"
ICECAST_PORT="${ICECAST_PORT:-8081}"
CHANNEL_MOUNT="${CHANNEL_MOUNT:-/${CHANNEL_NAME}}"
AUDIO_URL="${AUDIO_URL:-http://${ICECAST_HOST}:${ICECAST_PORT}${CHANNEL_MOUNT}}"

RELAY_RTMP="${RELAY_RTMP:-rtmp://mediamtx:1935}"
# The operator preview at <channel>/preview. Always published; the control
# plane's player and the channel card both read it.
PREVIEW_WIDTH="${PREVIEW_WIDTH:-640}"
PREVIEW_HEIGHT="${PREVIEW_HEIGHT:-360}"
PREVIEW_FPS="${PREVIEW_FPS:-15}"
# The internal video feed at <channel>/video, when PUBLISH_VIDEO is on.
LOCAL_WIDTH="${LOCAL_WIDTH:-$WIDTH}"
LOCAL_HEIGHT="${LOCAL_HEIGHT:-$HEIGHT}"
LOCAL_FPS="${LOCAL_FPS:-$FPS}"
# Each `off` is a relay path this composer never publishes, so that path never
# goes ready. mediamtx hangs the YouTube hook on the program path alone, so
# PUBLISH_PROGRAM=off makes reaching YouTube impossible rather than unlikely.
# Anything other than exactly "off" publishes, so a typo cannot silently take a
# channel off air.
PUBLISH_PROGRAM="${PUBLISH_PROGRAM:-on}"
PUBLISH_VIDEO="${PUBLISH_VIDEO:-}"
PUBLISH_AUDIO="${PUBLISH_AUDIO:-off}"
# A compose file written before delivery targets existed carries PUBLISH_PROGRAM
# but neither of the others, and that combination would otherwise mean "deliver
# nowhere" and refuse to start. Mirror the backend's migration instead: an
# old-style internal channel is a video channel.
if [[ -z "$PUBLISH_VIDEO" ]]; then
  if [[ "$PUBLISH_PROGRAM" == "off" ]]; then PUBLISH_VIDEO=on; else PUBLISH_VIDEO=off; fi
fi

# Standby is the overlay's timeline switch. The framekeeper keeps the stable
# layer alive whether or not a visualizer child exists.
VIZ_VISIBLE="${VIZ_VISIBLE:-on}"
if [[ "$VIZ_VISIBLE" == "off" ]]; then VIZ_ENABLE=0; else VIZ_ENABLE=1; fi
ACCENT="${ACCENT:-#4FC3F7}"
VIZ_OPACITY="${VIZ_OPACITY:-0.65}"

# auto: the producer derives color from each slide's profile. manual: the
# operator owns it and the backend sends the zmq commands, so the producer must
# stay out of the way or it overwrites them at the next slide change.
# Unset or unrecognized is auto, which is what an older compose file expects.
COLOR_MODE="${COLOR_MODE:-auto}"
case "$COLOR_MODE" in
  auto|manual) : ;;
  *) warn "COLOR_MODE='${COLOR_MODE}' unrecognized; using auto"; COLOR_MODE=auto ;;
esac

# A filtergraph is fixed at launch, so a manual color has to start baked into
# eq/hue: otherwise a composer restart silently drops it back to neutral. These
# values are interpolated straight into the graph, so anything non-numeric is
# either a launch failure or a filter injection — neutral is the safe answer.
# Ranges are zmq-control.md's.
color_init() {
  local name="$1" value="$2" low="$3" high="$4" neutral="$5"
  if [[ ! "$value" =~ ^-?([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
    warn "${name}='${value}' is not a number; using ${neutral}"
    printf '%s' "$neutral"
    return
  fi
  awk -v v="$value" -v lo="$low" -v hi="$high" \
    'BEGIN { if (v < lo) v = lo; if (v > hi) v = hi; printf "%g", v }'
}

INIT_HUE="$(color_init COLOR_INIT_HUE "${COLOR_INIT_HUE:-0}" -360 360 0)"
INIT_SATURATION="$(color_init COLOR_INIT_SATURATION "${COLOR_INIT_SATURATION:-1}" 0 3 1)"
INIT_BRIGHTNESS="$(color_init COLOR_INIT_BRIGHTNESS "${COLOR_INIT_BRIGHTNESS:-0}" -1 1 0)"
# Per-channel gamma is the only commandable, expression-capable way to put color
# *into* neutral content: hue and saturation can only rescale chroma that is
# already there, so on grey artwork they measure as no-ops.
INIT_GAMMA_R="$(color_init COLOR_INIT_GAMMA_R "${COLOR_INIT_GAMMA_R:-1}" 0.1 10 1)"
INIT_GAMMA_G="$(color_init COLOR_INIT_GAMMA_G "${COLOR_INIT_GAMMA_G:-1}" 0.1 10 1)"
INIT_GAMMA_B="$(color_init COLOR_INIT_GAMMA_B "${COLOR_INIT_GAMMA_B:-1}" 0.1 10 1)"
# The visualization's own grade, upstream of the blend. Automatic mode drives
# this so a slide's accent recolors the visualization without re-tinting the
# photograph the accent was sampled from.
INIT_VIZ_HUE="$(color_init COLOR_INIT_VIZ_HUE "${COLOR_INIT_VIZ_HUE:-0}" -360 360 0)"
INIT_VIZ_SATURATION="$(color_init COLOR_INIT_VIZ_SATURATION "${COLOR_INIT_VIZ_SATURATION:-1}" 0 10 1)"

# now.json's started_at. Taken once here so it is the compositor's start and
# not the producer's, which is a few seconds later.
STARTED_AT="${COMPOSER_STARTED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
LIQ_TELNET_HOST="${LIQ_TELNET_HOST:-${CHANNEL_NAME}-liquidsoap}"
LIQ_TELNET_PORT="${LIQ_TELNET_PORT:-1234}"

# NEVER tcp://*:5555. That default plus one malformed message is a remote kill
# of a live encoder — see docs/contracts/zmq-control.md.
ZMQ_BIND_HOST="${ZMQ_BIND_HOST:-127.0.0.1}"
ZMQ_BIND_PORT="${ZMQ_BIND_PORT:-5555}"

RUN_ROOT="${RUN_ROOT:-/run/ambient}"
[[ "$RUN_ROOT" == /* && ! -L "$RUN_ROOT" ]] || die "RUN_ROOT must be an absolute real directory"
EXPECTED_RUN_DIR="${RUN_ROOT%/}/${CHANNEL_NAME}"
RUN_DIR="${RUN_DIR:-$EXPECTED_RUN_DIR}"
[[ "$RUN_DIR" == "$EXPECTED_RUN_DIR" ]] || die "RUN_DIR must be $EXPECTED_RUN_DIR"
mkdir -p -- "$RUN_DIR"
[[ -d "$RUN_DIR" && ! -L "$RUN_DIR" ]] || die "RUN_DIR must be a real directory"
chmod 700 "$RUN_DIR"
PROGRESS_FILE="${PROGRESS_FILE:-${RUN_DIR}/${PROGRESS_NAME:-progress}}"
NOW_FILE="${NOW_FILE:-${RUN_DIR}/now.json}"
GRAPH_FILE="${RUN_DIR}/filtergraph.txt"
SLIDES_FIFO="${RUN_DIR}/slides.pipe"
VIZ_FIFO="${RUN_DIR}/visualization.pipe"
VIZ_SOCKET="${RUN_DIR}/visualization.sock"
VIZ_STATUS="${RUN_DIR}/visualization-status.json"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SLIDESHOW_BIN="${SLIDESHOW_BIN:-$(dirname "$0")/slideshow.py}"
FRAMEKEEPER_BIN="${FRAMEKEEPER_BIN:-$(dirname "$0")/framekeeper.py}"
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-level+warning}"

PRODUCER_PID=""
FRAMEKEEPER_PID=""
FFMPEG_PID=""

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in "$FFMPEG_PID" "$PRODUCER_PID" "$FRAMEKEEPER_PID"; do
    [[ -n "$pid" ]] || continue
    kill -INT "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    local live=0
    for pid in "$FFMPEG_PID" "$PRODUCER_PID" "$FRAMEKEEPER_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && live=1
    done
    [[ "$live" == 0 ]] && break
    sleep 0.25
  done
  for pid in "$FFMPEG_PID" "$PRODUCER_PID" "$FRAMEKEEPER_PID"; do
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
  done
  [[ -p "$SLIDES_FIFO" ]] && unlink "$SLIDES_FIFO" 2>/dev/null || true
  [[ -p "$VIZ_FIFO" ]] && unlink "$VIZ_FIFO" 2>/dev/null || true
  [[ -S "$VIZ_SOCKET" ]] && unlink "$VIZ_SOCKET" 2>/dev/null || true
  log "cleaned up (rc=$rc)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

SOURCE_REVISION="${SOURCE_REVISION:-${IMAGE_REVISION:-unknown}}"
[[ "$SOURCE_REVISION" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || SOURCE_REVISION=unknown
ok "runtime source=ffmpeg/entrypoint.sh revision=$SOURCE_REVISION"
ok "visualization layer=${LAYER_WIDTH}x${LAYER_HEIGHT}@${LAYER_FPS} stale=350ms"

# ------------------------------------------------------------- bitrate ladder
# youtube-ingest skill. Bufsize is always 2x video bitrate.
ladder() {
  case "$1" in
    [0-9]*) : ;;
    *) die "ladder: bad height '$1'" ;;
  esac
  if   (( $1 <= 144  )); then echo "400k 800k 96k"
  elif (( $1 <= 240  )); then echo "700k 1400k 96k"
  elif (( $1 <= 360  )); then echo "1000k 2000k 128k"
  elif (( $1 <= 480  )); then echo "1500k 3000k 128k"
  elif (( $1 <= 720  )); then echo "3000k 6000k 128k"
  elif (( $1 <= 1080 )); then echo "5000k 10000k 192k"
  elif (( $1 <= 1440 )); then echo "8000k 16000k 192k"
  else                        echo "16000k 32000k 256k"
  fi
}

read -r RATE BUFSIZE AUDIO_BR <<<"$(ladder "$HEIGHT")"
read -r P_RATE P_BUFSIZE P_AUDIO_BR <<<"$(ladder "$PREVIEW_HEIGHT")"
read -r L_RATE L_BUFSIZE L_AUDIO_BR <<<"$(ladder "$LOCAL_HEIGHT")"
GOP=$(( FPS * 2 ))
P_GOP=$(( PREVIEW_FPS * 2 ))
L_GOP=$(( LOCAL_FPS * 2 ))

# ------------------------------------------------------------- encoder profile
# youtube-ingest skill. The rate-control intent is the same for all three — CBR
# at the ladder rate, fixed GOP of fps*2, no scene-cut keyframes, yuv420p — only
# the knob names differ. -preset veryfast and -x264-params are libx264-only.
VIDEO_FLAGS=()
video_flags() {
  local encoder="$1" fps="$2" rate="$3" bufsize="$4" gop="$5"
  local -a codec rc_tail
  case "$encoder" in
    libx264)
      codec=(-c:v libx264 -preset "$PRESET")
      rc_tail=(-sc_threshold 0 -x264-params "nal-hrd=cbr:force-cfr=1")
      ;;
    h264_nvenc)
      codec=(-c:v h264_nvenc -preset p4 -tune ll -rc cbr -cbr 1)
      rc_tail=(-no-scenecut 1)
      ;;
    h264_qsv)
      # No -rc_mode: that option is h264_vaapi's, not h264_qsv's. CBR comes from
      # -b:v and -maxrate being equal below.
      codec=(-c:v h264_qsv -preset medium)
      rc_tail=()
      ;;
    *)
      die "ENCODER '$encoder' is not one of: libx264, h264_nvenc, h264_qsv"
      ;;
  esac
  VIDEO_FLAGS=(
    "${codec[@]}" -r "$fps" -fps_mode cfr
    -b:v "$rate" -minrate "$rate" -maxrate "$rate" -bufsize "$bufsize"
    -g "$gop" -keyint_min "$gop" "${rc_tail[@]}" -pix_fmt yuv420p
  )
}

video_flags "$ENCODER" "$FPS" "$RATE" "$BUFSIZE" "$GOP"
MAIN_VIDEO=("${VIDEO_FLAGS[@]}")

# The preview is a 360p operator view, not a product, and it defaults to
# libx264 because consumer NVENC caps concurrent sessions — spending one here
# would cost a whole channel. Quadro and datacenter cards have no such cap, so a
# host with one can set PREVIEW_ENCODER to move it to the GPU as well. Mixing
# encoders in one FFmpeg process is legal either way.
# Matches build_composer_command() in backend/ambient/ffmpeg_cmd.py.
PREVIEW_ENCODER="${PREVIEW_ENCODER:-libx264}"
case "$PREVIEW_ENCODER" in
  libx264|h264_nvenc|h264_qsv) ;;
  *) warn "PREVIEW_ENCODER '$PREVIEW_ENCODER' unrecognized; using libx264"
     PREVIEW_ENCODER=libx264 ;;
esac
video_flags "$PREVIEW_ENCODER" "$PREVIEW_FPS" "$P_RATE" "$P_BUFSIZE" "$P_GOP"
PREVIEW_VIDEO=("${VIDEO_FLAGS[@]}")

# The internal video feed is a product, not a preview, so it uses the channel's
# own encoder.
LOCAL_ENCODER="${LOCAL_ENCODER:-$ENCODER}"
video_flags "$LOCAL_ENCODER" "$LOCAL_FPS" "$L_RATE" "$L_BUFSIZE" "$L_GOP"
LOCAL_VIDEO=("${VIDEO_FLAGS[@]}")

DELIVERS=()
[[ "$PUBLISH_PROGRAM" != "off" ]] && DELIVERS+=("youtube ${ENCODER}@${RATE}")
[[ "$PUBLISH_VIDEO"   == "on"  ]] && DELIVERS+=("video ${LOCAL_ENCODER}@${L_RATE} ${LOCAL_WIDTH}x${LOCAL_HEIGHT}@${LOCAL_FPS}")
[[ "$PUBLISH_AUDIO"   == "on"  ]] && DELIVERS+=("audio ${AUDIO_BR}")
(( ${#DELIVERS[@]} )) || die "no delivery target: set at least one of PUBLISH_PROGRAM, PUBLISH_VIDEO, PUBLISH_AUDIO"
ok "delivery: ${DELIVERS[*]} (+ preview ${PREVIEW_ENCODER}@${P_RATE})"

# ------------------------------------------------------------- delivery fan-out
# The tap lists drive both the filtergraph's split/asplit and the output list,
# so the two can never disagree about how many branches exist. The operator
# preview is always present; the rest follow PUBLISH_*.
VIDEO_TAPS=()
AUDIO_TAPS=()
[[ "$PUBLISH_PROGRAM" != "off" ]] && { VIDEO_TAPS+=("vmain"); AUDIO_TAPS+=("amain"); }
VIDEO_TAPS+=("vpre"); AUDIO_TAPS+=("apreview")
[[ "$PUBLISH_VIDEO" == "on" ]] && { VIDEO_TAPS+=("vloc"); AUDIO_TAPS+=("alocal"); }
# Audio-only needs no video branch at all: that is the whole point of it.
[[ "$PUBLISH_AUDIO" == "on" ]] && AUDIO_TAPS+=("aonly")
VIDEO_BRANCHES=${#VIDEO_TAPS[@]}
AUDIO_BRANCHES=${#AUDIO_TAPS[@]}

# ----------------------------------------------------------------- filtergraph
# Written to a file so neither bash nor the filtergraph tokenizer has to survive
# the zmq bind_address escaping, which needs two levels.
#
# Color lives in two places, and the split is deliberate.
#
#   hue@viz    grades the visualization only, before the blend. Automatic mode
#              owns it: the accent is sampled *from* the current slide, so
#              re-tinting the slide with it is circular — the visualization is
#              the thing that has to move.
#   eq@eq +    grade the finished composite, after the blend. The operator owns
#   hue@hue    them. Measured: with these on [base] instead, the operator's
#              color could only ever tint the photograph *behind* the
#              visualization, and the visualization is the brightest element in
#              the frame — which is why the channel looked stuck.
{
  printf '%s' "[1:v]fps=${FPS}:start_time=0,realtime,"
  printf '%s' "zmq@ctl=bind_address=tcp\\\\://${ZMQ_BIND_HOST}\\\\:${ZMQ_BIND_PORT},"
  printf '%s' "format=yuv420p,setsar=1[base];"
  printf '%s' "[2:v]scale=${WIDTH}:${HEIGHT}:flags=fast_bilinear,fps=${FPS},realtime,"
  printf '%s' "hue@viz=h=${INIT_VIZ_HUE}:s=${INIT_VIZ_SATURATION},split=2[vizc][vizm];"
  # The visualization draws on black, so its own luma is its coverage. Turning
  # that into a real alpha channel is what lets a bar be the accent color rather
  # than merely tint whatever is behind it: measured, an additive chroma blend
  # rendered green bars over orange artwork as brighter orange, because adding a
  # chroma offset cannot replace the chroma already there.
  printf '%s' "[vizm]format=gray,lut@vizop=y='(val-16)*1.16438356*${VIZ_OPACITY}'[vizalpha];"
  printf '%s' "[vizc][vizalpha]alphamerge[vizrgba];"
  # enable is timeline, so the backend can bypass the overlay on the running
  # graph in one frame.
  printf '%s' "[base][vizrgba]overlay@viz=eof_action=pass:format=auto:enable=${VIZ_ENABLE},"
  # eval=frame is not commandable, so it can only be set here.
  printf '%s' "eq@eq=eval=frame:contrast=1:brightness=${INIT_BRIGHTNESS}:saturation=${INIT_SATURATION}"
  printf '%s' ":gamma_r=${INIT_GAMMA_R}:gamma_g=${INIT_GAMMA_G}:gamma_b=${INIT_GAMMA_B},"
  printf '%s' "hue@hue=h=${INIT_HUE},format=yuv420p,setsar=1[vfull];"
  # Fan out to exactly the branches that have an output. An unused branch is a
  # whole extra scale+encode, which on a busy host is a channel's worth of CPU.
  printf '%s' "[vfull]split=${VIDEO_BRANCHES}"
  for label in "${VIDEO_TAPS[@]}"; do printf '[%s]' "$label"; done
  printf '%s' ";"
  printf '%s' "[vpre]scale=${PREVIEW_WIDTH}:${PREVIEW_HEIGHT}:flags=fast_bilinear,fps=${PREVIEW_FPS}[vpreview];"
  if [[ "$PUBLISH_VIDEO" == "on" ]]; then
    if [[ "$LOCAL_WIDTH" == "$WIDTH" && "$LOCAL_HEIGHT" == "$HEIGHT" ]]; then
      printf '%s' "[vloc]fps=${LOCAL_FPS}[vlocal];"
    else
      printf '%s' "[vloc]scale=${LOCAL_WIDTH}:${LOCAL_HEIGHT}:flags=fast_bilinear,fps=${LOCAL_FPS}[vlocal];"
    fi
  fi
  printf '%s' "[0:a]aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11"
  printf '%s' ",asplit=${AUDIO_BRANCHES}"
  for label in "${AUDIO_TAPS[@]}"; do printf '[%s]' "$label"; done
} > "$GRAPH_FILE"
log "filtergraph -> $GRAPH_FILE ($(wc -c < "$GRAPH_FILE") bytes)"
# The active visualizer bakes this accent at its own launch. Publishing the
# baseline lets the backend compute live hue rotation without guessing.
printf '%s\n' "$ACCENT" > "${RUN_DIR}/viz-accent"
ok "color: ${COLOR_MODE} (composite hue=${INIT_HUE} saturation=${INIT_SATURATION} brightness=${INIT_BRIGHTNESS} gamma=${INIT_GAMMA_R}/${INIT_GAMMA_G}/${INIT_GAMMA_B}; viz hue=${INIT_VIZ_HUE} saturation=${INIT_VIZ_SATURATION})"

# -------------------------------------------------------------------- producer
# The producer also writes now.json: it owns the current slide, and it is the
# only long-lived Python process here, so it merges Liquidsoap's track state.
for pipe in "$SLIDES_FIFO" "$VIZ_FIFO"; do
  [[ ! -e "$pipe" ]] || unlink "$pipe"
  mkfifo -m 600 "$pipe"
done

"$PYTHON_BIN" "$FRAMEKEEPER_BIN" \
  --input "$VIZ_SOCKET" \
  --width "$LAYER_WIDTH" --height "$LAYER_HEIGHT" --fps "$LAYER_FPS" \
  --stale-seconds 0.35 --status "$VIZ_STATUS" \
  > "$VIZ_FIFO" &
FRAMEKEEPER_PID=$!
ok "framekeeper pid $FRAMEKEEPER_PID -> $VIZ_FIFO"

CHANNEL_NAME="$CHANNEL_NAME" \
RUN_DIR="$RUN_DIR" \
NOW_FILE="$NOW_FILE" \
COMPOSER_STARTED_AT="$STARTED_AT" \
LIQ_TELNET_HOST="$LIQ_TELNET_HOST" \
LIQ_TELNET_PORT="$LIQ_TELNET_PORT" \
"$PYTHON_BIN" "$SLIDESHOW_BIN" \
  --width "$WIDTH" --height "$HEIGHT" --fps "$PRODUCER_FPS" \
  --zmq-endpoint "tcp://${ZMQ_BIND_HOST}:${ZMQ_BIND_PORT}" \
  --color-mode "$COLOR_MODE" \
  --baked-accent "$ACCENT" \
  > "$SLIDES_FIFO" &
PRODUCER_PID=$!
ok "producer pid $PRODUCER_PID at ${PRODUCER_FPS} fps, ${WIDTH}x${HEIGHT}"
log "now.json -> $NOW_FILE (liquidsoap ${LIQ_TELNET_HOST}:${LIQ_TELNET_PORT})"

# ------------------------------------------------------------------- compositor
# -reconnect_on_network_error 1 is MANDATORY: compose starts both containers at
# once, so the first connect always finds nothing listening and plain
# -reconnect only covers a drop mid-stream.
# -probesize/-analyzeduration: without them FFmpeg takes 8.4 s to first sample.
# -fflags nobuffer: 0.45s of a 0.9s cold start, and every second before the first
# packet is dead air, because the replacement evicts the outgoing composer the
# moment it connects. It was worth nothing until Icecast began bursting - before
# that the wait was for audio to arrive at all, not for FFmpeg to buffer it.
# stdin is the producer FIFO, never a terminal, so </dev/null is not used here.
# The relay path a channel publishes to decides whether it can reach YouTube:
# mediamtx.yml hangs the publisher hook on the program path only, and the
# preview/video/audio paths are documented as ones that must never get there.
OUTPUTS=()
PUBLISHED=()
if [[ "$PUBLISH_PROGRAM" != "off" ]]; then
  OUTPUTS+=(
    -map '[vmain]' -map '[amain]'
      "${MAIN_VIDEO[@]}"
      -c:a aac -b:a "$AUDIO_BR" -ar 44100
      -f flv "${RELAY_RTMP}/${CHANNEL_NAME}"
  )
  PUBLISHED+=("${CHANNEL_NAME}")
fi
OUTPUTS+=(
  -map '[vpreview]' -map '[apreview]'
    "${PREVIEW_VIDEO[@]}"
    -c:a aac -b:a "$P_AUDIO_BR" -ar 44100
    -f flv "${RELAY_RTMP}/${CHANNEL_NAME}/preview"
)
PUBLISHED+=("${CHANNEL_NAME}/preview")
if [[ "$PUBLISH_VIDEO" == "on" ]]; then
  OUTPUTS+=(
    -map '[vlocal]' -map '[alocal]'
      "${LOCAL_VIDEO[@]}"
      -c:a aac -b:a "$L_AUDIO_BR" -ar 44100
      -f flv "${RELAY_RTMP}/${CHANNEL_NAME}/video"
  )
  PUBLISHED+=("${CHANNEL_NAME}/video")
fi
if [[ "$PUBLISH_AUDIO" == "on" ]]; then
  # No -map for video at all. FLV carries an audio-only stream, and the relay
  # serves it as an audio-only HLS rendition.
  OUTPUTS+=(
    -map '[aonly]'
      -c:a aac -b:a "$AUDIO_BR" -ar 44100
      -f flv "${RELAY_RTMP}/${CHANNEL_NAME}/audio"
  )
  PUBLISHED+=("${CHANNEL_NAME}/audio")
fi

"$FFMPEG_BIN" -nostdin -hide_banner -loglevel "$FFMPEG_LOGLEVEL" \
  -progress "$PROGRESS_FILE" \
  -probesize 32k -analyzeduration 500000 -fflags nobuffer \
  -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 \
  -reconnect_delay_max 5 \
  -i "$AUDIO_URL" \
  -f image2pipe -framerate "$PRODUCER_FPS" -i pipe:0 \
  -f rawvideo -pixel_format yuv420p \
  -video_size "${LAYER_WIDTH}x${LAYER_HEIGHT}" -framerate "$LAYER_FPS" \
  -i "$VIZ_FIFO" \
  -filter_complex_script "$GRAPH_FILE" \
  "${OUTPUTS[@]}" \
  < "$SLIDES_FIFO" &
FFMPEG_PID=$!
ok "ffmpeg pid $FFMPEG_PID -> ${PUBLISHED[*]}"

# Producer death closes the FIFO, so waiting on FFmpeg covers both halves of
# the supervised unit. Any exit is a fault; the supervisor decides what next.
wait "$FFMPEG_PID"
