#!/usr/bin/env bash
# Compositor entrypoint: slideshow producer -> FFmpeg -> MediaMTX (full + preview).
#
# This process must run for the life of the channel. A restart is a new YouTube
# ingest session, so nothing here may depend on relaunching FFmpeg: images
# arrive on image2pipe, colors change over zmq, plugins switch via streamselect.
#
# See docs/contracts/{audio-transport,slideshow,plugin,zmq-control}.md and the
# youtube-ingest skill. Encoder flags are verbatim from that skill.
set -euo pipefail

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[..]%s composer %s\n' "$BLU" "$NC" "$*" >&2; }
ok()   { printf '%s[ok]%s composer %s\n' "$GRN" "$NC" "$*" >&2; }
warn() { printf '%s[!!]%s composer %s\n' "$YLW" "$NC" "$*" >&2; }
die()  { printf '%s[XX]%s composer %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- environment
CHANNEL_NAME="${CHANNEL_NAME:-}"
[[ -n "$CHANNEL_NAME" ]] || die "CHANNEL_NAME is required"

WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-30}"
PRODUCER_FPS="${PRODUCER_FPS:-10}"
ENCODER="${ENCODER:-libx264}"
PRESET="${X264_PRESET:-veryfast}"

ICECAST_HOST="${ICECAST_HOST:-icecast}"
ICECAST_PORT="${ICECAST_PORT:-8081}"
CHANNEL_MOUNT="${CHANNEL_MOUNT:-/${CHANNEL_NAME}}"
AUDIO_URL="${AUDIO_URL:-http://${ICECAST_HOST}:${ICECAST_PORT}${CHANNEL_MOUNT}}"

RELAY_RTMP="${RELAY_RTMP:-rtmp://mediamtx:1935}"
PREVIEW_WIDTH="${PREVIEW_WIDTH:-640}"
PREVIEW_HEIGHT="${PREVIEW_HEIGHT:-360}"
PREVIEW_FPS="${PREVIEW_FPS:-15}"

PLUGIN_DIR="${PLUGIN_DIR:-/plugins}"
HOT_SET="${HOT_SET:-showfreqs-bars}"
ACTIVE_PLUGIN="${ACTIVE_PLUGIN:-}"
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

RUN_DIR="${RUN_DIR:-/run/ambient/${CHANNEL_NAME}}"
PROGRESS_FILE="${PROGRESS_FILE:-${RUN_DIR}/progress}"
NOW_FILE="${NOW_FILE:-${RUN_DIR}/now.json}"
GRAPH_FILE="${RUN_DIR}/filtergraph.txt"
FIFO="${RUN_DIR}/slides.pipe"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SLIDESHOW_BIN="${SLIDESHOW_BIN:-$(dirname "$0")/slideshow.py}"
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
FFMPEG_LOGLEVEL="${FFMPEG_LOGLEVEL:-level+warning}"

PRODUCER_PID=""
FFMPEG_PID=""

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in "$FFMPEG_PID" "$PRODUCER_PID"; do
    [[ -n "$pid" ]] || continue
    kill -INT "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    local live=0
    for pid in "$FFMPEG_PID" "$PRODUCER_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && live=1
    done
    [[ "$live" == 0 ]] && break
    sleep 0.25
  done
  for pid in "$FFMPEG_PID" "$PRODUCER_PID"; do
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
  done
  [[ -p "$FIFO" ]] && unlink "$FIFO" 2>/dev/null || true
  log "cleaned up (rc=$rc)"
  exit "$rc"
}
trap cleanup EXIT INT TERM

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
GOP=$(( FPS * 2 ))
P_GOP=$(( PREVIEW_FPS * 2 ))

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

# The preview is a 360p operator view, not a product. Consumer NVENC caps
# concurrent sessions, so spending one here costs a whole channel; mixing
# encoders in one FFmpeg process is legal, so it stays on libx264 regardless
# of $ENCODER. Matches build_composer_command() in backend/ambient/ffmpeg_cmd.py.
video_flags libx264 "$PREVIEW_FPS" "$P_RATE" "$P_BUFSIZE" "$P_GOP"
PREVIEW_VIDEO=("${VIDEO_FLAGS[@]}")
ok "encoder: ${ENCODER} @ ${RATE} (preview libx264 @ ${P_RATE})"

# --------------------------------------------------------------------- plugins
# ffmpeg takes colors as 0xRRGGBB; '#' is a filtergraph escaping problem.
ACCENT_FF="0x${ACCENT#\#}"

IFS=',' read -r -a PLUGINS <<<"$HOT_SET"
(( ${#PLUGINS[@]} > 0 )) || die "HOT_SET is empty"

AVAILABLE_FILTERS="$("$FFMPEG_BIN" -hide_banner -loglevel error -filters </dev/null | awk '{print $2}')"

ACTIVE_INDEX=0
VIZ_FRAGMENTS=""
VIZ_LABELS=""
for i in "${!PLUGINS[@]}"; do
  name="${PLUGINS[$i]}"
  frag="${PLUGIN_DIR}/${name}/viz.ffmpeg"
  manifest="${PLUGIN_DIR}/${name}/config.json"
  [[ -f "$frag" ]] || die "plugin '$name' has no viz.ffmpeg at $frag"
  [[ -f "$manifest" ]] || die "plugin '$name' has no config.json"

  # The exact-size rule: FFmpeg silently corrupts a mis-sized branch, exit 0,
  # no error. A fragment that does not take its size from the channel cannot
  # be proven correct, so refuse it.
  grep -q '\${WIDTH}' "$frag" && grep -q '\${HEIGHT}' "$frag" \
    || die "plugin '$name' does not derive its size from \${WIDTH}x\${HEIGHT}"

  while read -r required; do
    [[ -n "$required" ]] || continue
    grep -qx "$required" <<<"$AVAILABLE_FILTERS" \
      || die "plugin '$name' requires filter '$required', absent from this build"
  done < <("$PYTHON_BIN" -c 'import json,sys;print("\n".join(json.load(open(sys.argv[1])).get("requires_filters",[])))' "$manifest")

  [[ "$name" == "$ACTIVE_PLUGIN" ]] && ACTIVE_INDEX="$i"

  body="$(tr '\n' ' ' < "$frag" | sed 's/  */ /g; s/^ //; s/ $//')"
  body="${body//\$\{WIDTH\}/$WIDTH}"
  body="${body//\$\{HEIGHT\}/$HEIGHT}"
  body="${body//\$\{FPS\}/$FPS}"
  body="${body//\$\{ACCENT\}/$ACCENT_FF}"
  body="${body//\$\{OUT\}/viz$i}"
  VIZ_FRAGMENTS+="${body};"
  VIZ_LABELS+="[viz$i]"
done
ok "plugins: ${HOT_SET} (active index ${ACTIVE_INDEX}, ${#PLUGINS[@]} hot branches)"

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
mkdir -p "$RUN_DIR"
{
  # Input 0 is audio so a plugin fragment's literal [0:a] is correct as written.
  printf '%s' "[1:v]fps=${FPS}:start_time=0,realtime,"
  printf '%s' "zmq@ctl=bind_address=tcp\\\\://${ZMQ_BIND_HOST}\\\\:${ZMQ_BIND_PORT},"
  printf '%s' "format=yuv420p,setsar=1[base];"
  printf '%s' "$VIZ_FRAGMENTS"
  # streamselect rejects inputs=1 (range is 2..INT_MAX), so a single hot plugin
  # has no selector — there is nothing to switch to. See the report to the lead.
  if (( ${#PLUGINS[@]} > 1 )); then
    printf '%s' "${VIZ_LABELS}streamselect@sel=inputs=${#PLUGINS[@]}:map=${ACTIVE_INDEX},"
  else
    printf '%s' "[viz0]"
  fi
  # One instance downstream of the selector, so it survives a plugin switch.
  # hue carries h, s and b, which is every grade the visualization needs.
  printf '%s' "hue@viz=h=${INIT_VIZ_HUE}:s=${INIT_VIZ_SATURATION}[viz];"
  # NOT all_mode=screen. Screen's identity is 0, but chroma's neutral is 128, so
  # screening U and V drove both to ~192 and clipped R and B at 255 — a magenta
  # cast that no upstream color change could survive. Measured on a #E8A0C0
  # field: background (255,122,255), and `eq saturation 0` moved it only to
  # (252,139,255). Luma keeps screen; chroma gets grainmerge, which is
  # base + (viz - 128) — an additive chroma offset that is a true no-op wherever
  # the visualization is black.
  printf '%s' "[base][viz]blend=c0_mode=screen:c1_mode=grainmerge:c2_mode=grainmerge"
  printf '%s' ":all_opacity=${VIZ_OPACITY},"
  # eval=frame is not commandable, so it can only be set here.
  printf '%s' "eq@eq=eval=frame:contrast=1:brightness=${INIT_BRIGHTNESS}:saturation=${INIT_SATURATION}"
  printf '%s' ":gamma_r=${INIT_GAMMA_R}:gamma_g=${INIT_GAMMA_G}:gamma_b=${INIT_GAMMA_B},"
  printf '%s' "hue@hue=h=${INIT_HUE},format=yuv420p,setsar=1[vfull];"
  printf '%s' "[vfull]split=2[vmain][vpre];"
  printf '%s' "[vpre]scale=${PREVIEW_WIDTH}:${PREVIEW_HEIGHT}:flags=fast_bilinear,fps=${PREVIEW_FPS}[vpreview];"
  printf '%s' "[0:a]aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11,asplit=2[amain][apreview]"
} > "$GRAPH_FILE"
log "filtergraph -> $GRAPH_FILE ($(wc -c < "$GRAPH_FILE") bytes)"
# The accent is baked into the plugins at launch, so a live change has to be a
# rotation away from this value. Publishing it is what lets the backend compute
# that delta without guessing what the graph was built with.
printf '%s\n' "$ACCENT" > "${RUN_DIR}/viz-accent"
ok "color: ${COLOR_MODE} (composite hue=${INIT_HUE} saturation=${INIT_SATURATION} brightness=${INIT_BRIGHTNESS} gamma=${INIT_GAMMA_R}/${INIT_GAMMA_G}/${INIT_GAMMA_B}; viz hue=${INIT_VIZ_HUE} saturation=${INIT_VIZ_SATURATION})"

# -------------------------------------------------------------------- producer
# The producer also writes now.json: it owns the current slide, and it is the
# only long-lived Python process here, so it merges Liquidsoap's track state
# and the active plugin into one file for the control plane. See nowstate.py.
[[ -p "$FIFO" ]] || mkfifo "$FIFO"
CHANNEL_NAME="$CHANNEL_NAME" \
RUN_DIR="$RUN_DIR" \
NOW_FILE="$NOW_FILE" \
ACTIVE_PLUGIN="${PLUGINS[$ACTIVE_INDEX]}" \
COMPOSER_STARTED_AT="$STARTED_AT" \
LIQ_TELNET_HOST="$LIQ_TELNET_HOST" \
LIQ_TELNET_PORT="$LIQ_TELNET_PORT" \
"$PYTHON_BIN" "$SLIDESHOW_BIN" \
  --width "$WIDTH" --height "$HEIGHT" --fps "$PRODUCER_FPS" \
  --zmq-endpoint "tcp://${ZMQ_BIND_HOST}:${ZMQ_BIND_PORT}" \
  --color-mode "$COLOR_MODE" \
  > "$FIFO" &
PRODUCER_PID=$!
ok "producer pid $PRODUCER_PID at ${PRODUCER_FPS} fps, ${WIDTH}x${HEIGHT}"
log "now.json -> $NOW_FILE (liquidsoap ${LIQ_TELNET_HOST}:${LIQ_TELNET_PORT})"

# ------------------------------------------------------------------- compositor
# -reconnect_on_network_error 1 is MANDATORY: compose starts both containers at
# once, so the first connect always finds nothing listening and plain
# -reconnect only covers a drop mid-stream.
# -probesize/-analyzeduration: without them FFmpeg takes 8.4 s to first sample.
# stdin is the producer FIFO, never a terminal, so </dev/null is not used here.
"$FFMPEG_BIN" -nostdin -hide_banner -loglevel "$FFMPEG_LOGLEVEL" \
  -progress "$PROGRESS_FILE" \
  -probesize 32k -analyzeduration 500000 \
  -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 \
  -reconnect_delay_max 5 \
  -i "$AUDIO_URL" \
  -f image2pipe -framerate "$PRODUCER_FPS" -i pipe:0 \
  -filter_complex_script "$GRAPH_FILE" \
  -map '[vmain]' -map '[amain]' \
    "${MAIN_VIDEO[@]}" \
    -c:a aac -b:a "$AUDIO_BR" -ar 44100 \
    -f flv "${RELAY_RTMP}/${CHANNEL_NAME}" \
  -map '[vpreview]' -map '[apreview]' \
    "${PREVIEW_VIDEO[@]}" \
    -c:a aac -b:a "$P_AUDIO_BR" -ar 44100 \
    -f flv "${RELAY_RTMP}/${CHANNEL_NAME}/preview" \
  < "$FIFO" &
FFMPEG_PID=$!
ok "ffmpeg pid $FFMPEG_PID -> ${RELAY_RTMP}/${CHANNEL_NAME} (+ /preview)"

# Producer death closes the FIFO, so waiting on FFmpeg covers both halves of
# the supervised unit. Any exit is a fault; the supervisor decides what next.
wait "$FFMPEG_PID"
