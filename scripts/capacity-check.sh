#!/usr/bin/env bash
# ambient-streamer — will this host survive one more channel?
#
# Budgets from the MEASURED cost of a running channel, never from filtergraph
# benchmarks. A 720p channel with one hot plugin measured ~1.5 cores, because
# a channel is far more than its visualization branch: the HLS preview is a
# second complete encode, plus MP3 decode off Icecast and JPEG decode off
# image2pipe. A filter benchmark sees ~0.24 of that and under-predicts by 6x.
# See docs/scaling.md.
#
#   scripts/capacity-check.sh
#   scripts/capacity-check.sh --add 2      # project two more channels first
#
# Exit 0 within budget (warnings are still exit 0), 1 when the host is
# oversubscribed or already at limits.max_channels.
#
# Reports only. It starts nothing, stops nothing and writes nothing.
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

# ---------------------------------------------------------------------------
# Measured constants. Changing one means re-running the measurement.
# ---------------------------------------------------------------------------

# Measured on a 7-core host: composer 137-142% of a core, Liquidsoap ~6%, plus
# a share of the global MediaMTX (~7%, publisher included) and Icecast (~0.2%).
BASE_CORES_720P=1.5

# Each additional HOT plugin, on screen or not — hot_set is a CPU budget.
PLUGIN_CORES=0.28

# Share of BASE_CORES_720P that does not scale with output resolution: the
# 360p preview encode, MP3 decode and the publisher. The rest is scaled by
# pixel count, which is the only defensible extrapolation — 720p is the one
# resolution actually measured.
FIXED_SHARE=0.30

FAILED=0
WARNINGS=0
ADD=0

note_warning() { WARNINGS=$((WARNINGS + 1)); warn "$@"; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--add)
			shift
			[[ ${1:-} =~ ^[0-9]+$ ]] || die "--add takes a channel count, e.g. --add 2"
			ADD="$1" ;;
		-h|--help)
			sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
			exit 0 ;;
		*) die "unknown argument: $1 (try --help)" ;;
	esac
	shift
done

YAML_FILE="$REPO_ROOT/ambient.yaml"
[[ -f "$YAML_FILE" ]] || YAML_FILE="$REPO_ROOT/ambient.yaml.example"
[[ -f "$YAML_FILE" ]] || die "neither ambient.yaml nor ambient.yaml.example found"

# Flat key lookup. Every key read here is unique in the file, so no nesting
# awareness is needed — and a YAML parser is not worth a dependency in a
# script whose whole job is to run before anything is installed.
yaml_value() {
	sed -n -E "s/^[[:space:]]*$1:[[:space:]]*([^[:space:]#]+).*/\1/p" "$YAML_FILE" | head -1
}

env_value() {
	[[ -f "$1" ]] || return 0
	sed -n -E "s/^[[:space:]]*$2=(.*)$/\1/p" "$1" | tr -d '\r' | tail -n1
}

# Pixels per frame. Used only as a ratio against 720p.
res_pixels() {
	case "$1" in
		480p)  echo 409920 ;;
		720p)  echo 921600 ;;
		1080p) echo 2073600 ;;
		1440p) echo 3686400 ;;
		2160p) echo 8294400 ;;
		*)     echo 921600 ;;
	esac
}

# Sustained upstream to YouTube at 30 fps, Mbps, video + audio.
# 480p-1080p are the shipped bitrate ladder (docs/scaling.md); 1440p and 2160p
# are YouTube's recommended ranges. 50+ fps multiplies these by 1.5, which is
# what puts 4K60 at the ~51-68 Mbps YouTube asks for.
res_mbps() {
	case "$1" in
		480p)  echo 1.6 ;;
		720p)  echo 3.1 ;;
		1080p) echo 5.2 ;;
		1440p) echo 16 ;;
		2160p) echo 40 ;;
		*)     echo 3.1 ;;
	esac
}

# Entries under visualization.hot_set, block or inline form. Floors at 1: the
# measured 1.5 cores was itself a channel with one hot plugin.
hot_plugin_count() {
	local cfg="$1"
	[[ -f "$cfg" ]] || { echo 1; return; }
	awk '
		/^[[:space:]]*hot_set:[[:space:]]*\[/ {
			line = $0
			sub(/^[^[]*\[/, "", line); sub(/\].*$/, "", line)
			gsub(/[[:space:]]/, "", line)
			n = (line == "" ? 0 : split(line, parts, ","))
			exit
		}
		/^[[:space:]]*hot_set:[[:space:]]*$/ { inset = 1; next }
		inset && /^[[:space:]]*(#.*)?$/       { next }
		inset && /^[[:space:]]+-[[:space:]]*[^[:space:]]/ { n++; next }
		inset                                 { inset = 0 }
		END { print (n > 0 ? n : 1) }
	' "$cfg"
}

CORES="$(nproc)"
RESERVED="$(yaml_value reserved_cores)";   : "${RESERVED:=1.0}"
MAX_CHANNELS="$(yaml_value max_channels)"; : "${MAX_CHANNELS:=8}"
DEFAULT_RES="$(yaml_value resolution)";    : "${DEFAULT_RES:=720p}"
DEFAULT_FPS="$(yaml_value fps)";           : "${DEFAULT_FPS:=30}"
DEFAULT_ENCODER="$(yaml_value encoder)";   : "${DEFAULT_ENCODER:=libx264}"

# ---------------------------------------------------------------------------
# 1. Channels
# ---------------------------------------------------------------------------
head1 "1/4  channels"

RUNNING=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
	# Per-channel compose projects are named ambient-<channel>; the global
	# stack is plain 'ambient' and does not match.
	RUNNING="$(docker ps --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
		| sed -n 's/^ambient-\(..*\)$/\1/p' | sort -u || true)"
else
	note_warning "docker is not answering — running-channel count is unavailable"
fi

CONFIGURED=0
NVENC_REQUESTED=0
HAS_4K=0
TOTAL_CORES=0
TOTAL_MBPS=0

shopt -s nullglob
for channel_dir in "$REPO_ROOT"/channels/*/; do
	name="$(basename "$channel_dir")"
	[[ "$name" == "example" ]] && continue
	[[ -f "$channel_dir/.env" ]] || continue
	CONFIGURED=$((CONFIGURED + 1))

	res="$(env_value "$channel_dir/.env" CHANNEL_RESOLUTION)";  : "${res:=$DEFAULT_RES}"
	fps="$(env_value "$channel_dir/.env" CHANNEL_FPS)";         : "${fps:=$DEFAULT_FPS}"
	enc="$(env_value "$channel_dir/.env" CHANNEL_ENCODER)";     : "${enc:=$DEFAULT_ENCODER}"
	plugins="$(hot_plugin_count "$channel_dir/config.yaml")"
	[[ "$enc" == "h264_nvenc" ]] && NVENC_REQUESTED=$((NVENC_REQUESTED + 1))
	[[ "$res" == "2160p" ]] && HAS_4K=1

	read -r cores mbps < <(awk -v base="$BASE_CORES_720P" -v per="$PLUGIN_CORES" \
		-v fixed="$FIXED_SHARE" -v px="$(res_pixels "$res")" -v px720="$(res_pixels 720p)" \
		-v plugins="$plugins" -v fps="$fps" -v mbps="$(res_mbps "$res")" 'BEGIN {
			ratio = fixed + (1 - fixed) * (px / px720)
			# Roughly linear in frame rate on the filter branches.
			cores = (base + per * (plugins - 1)) * ratio * (fps / 30)
			if (fps >= 50) mbps = mbps * 1.5
			printf "%.2f %.1f\n", cores, mbps
		}')
	TOTAL_CORES="$(awk -v a="$TOTAL_CORES" -v b="$cores" 'BEGIN { printf "%.2f", a + b }')"
	TOTAL_MBPS="$(awk -v a="$TOTAL_MBPS" -v b="$mbps" 'BEGIN { printf "%.1f", a + b }')"

	state="stopped"
	grep -qx "$name" <<<"$RUNNING" && state="running"
	printf '     %-16s %-6s %sfps  %-11s %s hot  %5s cores  %5s Mbps  %s\n' \
		"$name" "$res" "$fps" "$enc" "$plugins" "$cores" "$mbps" "$state" >&2
done
shopt -u nullglob

RUNNING_COUNT=0
[[ -n "$RUNNING" ]] && RUNNING_COUNT="$(grep -c . <<<"$RUNNING")"

if (( ADD > 0 )); then
	read -r add_cores add_mbps < <(awk -v base="$BASE_CORES_720P" -v fixed="$FIXED_SHARE" \
		-v px="$(res_pixels "$DEFAULT_RES")" -v px720="$(res_pixels 720p)" \
		-v fps="$DEFAULT_FPS" -v mbps="$(res_mbps "$DEFAULT_RES")" -v n="$ADD" 'BEGIN {
			ratio = fixed + (1 - fixed) * (px / px720)
			c = base * ratio * (fps / 30)
			if (fps >= 50) mbps = mbps * 1.5
			printf "%.2f %.1f\n", c * n, mbps * n
		}')
	TOTAL_CORES="$(awk -v a="$TOTAL_CORES" -v b="$add_cores" 'BEGIN { printf "%.2f", a + b }')"
	TOTAL_MBPS="$(awk -v a="$TOTAL_MBPS" -v b="$add_mbps" 'BEGIN { printf "%.1f", a + b }')"
	CONFIGURED=$((CONFIGURED + ADD))
	log "including $ADD hypothetical ${DEFAULT_RES} channel(s): +$add_cores cores, +$add_mbps Mbps"
fi

ok "$CONFIGURED configured, $RUNNING_COUNT running, limits.max_channels $MAX_CHANNELS"

if (( CONFIGURED > MAX_CHANNELS )); then
	FAILED=1
	warn "$CONFIGURED channels exceeds limits.max_channels ($MAX_CHANNELS) in ambient.yaml.
       Nothing above 8 has been tested; the backend refuses the extra ones."
elif (( CONFIGURED == MAX_CHANNELS )); then
	note_warning "at limits.max_channels ($MAX_CHANNELS) — the next channel will be refused"
fi

# ---------------------------------------------------------------------------
# 2. Cores
# ---------------------------------------------------------------------------
head1 "2/4  cores"

AVAILABLE="$(awk -v c="$CORES" -v r="$RESERVED" 'BEGIN { printf "%.2f", c - r }')"
ok "$CORES cores, $RESERVED reserved for the OS and the shared containers -> $AVAILABLE available"
ok "projected $TOTAL_CORES cores for $CONFIGURED channel(s)"

HEADROOM="$(awk -v a="$AVAILABLE" -v u="$TOTAL_CORES" 'BEGIN { printf "%.2f", a - u }')"
ONE_MORE="$(awk -v base="$BASE_CORES_720P" -v fixed="$FIXED_SHARE" \
	-v px="$(res_pixels "$DEFAULT_RES")" -v px720="$(res_pixels 720p)" -v fps="$DEFAULT_FPS" \
	'BEGIN { printf "%.2f", base * (fixed + (1 - fixed) * (px / px720)) * (fps / 30) }')"
ROOM="$(awk -v h="$HEADROOM" -v c="$ONE_MORE" 'BEGIN { n = int(h / c); print (n < 0 ? 0 : n) }')"

case "$(awk -v u="$TOTAL_CORES" -v a="$AVAILABLE" 'BEGIN {
		if (u > a) print "over"; else if (u > a * 0.85) print "tight"; else print "fine" }')" in
	over)
		FAILED=1
		warn "OVERSUBSCRIBED — $TOTAL_CORES projected against $AVAILABLE available.
       Every channel drops below 1.0x together, and YouTube starves on all of
       them at once. Stop a channel, drop a hot plugin, or lower a resolution." ;;
	tight)
		note_warning "within 15% of the budget ($TOTAL_CORES of $AVAILABLE). Adding anything
       here is a coin flip; watch 'speed' on the EXISTING channels, not the new one." ;;
	fine)
		ok "$HEADROOM cores headroom — room for about $ROOM more ${DEFAULT_RES} channel(s)" ;;
esac

log "projection only. The real signal is FFmpeg 'speed' staying at 1.0x on every"
log "channel — a host at 95% CPU all at 1.0x is fine, one at 70% with a channel"
log "at 0.94x is not. See docs/scaling.md."

# ---------------------------------------------------------------------------
# 3. NVENC
# ---------------------------------------------------------------------------
head1 "3/4  nvenc"

if ! command -v nvidia-smi >/dev/null 2>&1; then
	ok "no NVIDIA GPU visible — every channel encodes on the CPU, which is what
       the core budget above already assumes"
elif ! nvidia-smi -L >/dev/null 2>&1; then
	note_warning "nvidia-smi is installed but failed. FFmpeg will still advertise NVENC and
       fail at runtime; treat this host as CPU-only until it is fixed."
else
	while IFS=, read -r gpu_name driver sessions; do
		gpu_name="${gpu_name# }"; driver="${driver# }"; sessions="${sessions# }"
		ok "$gpu_name (driver $driver)"
		[[ "$sessions" =~ ^[0-9]+$ ]] \
			&& log "encoder sessions in use right now: $sessions" \
			|| log "encoder session count not reported by this driver"

		case "$gpu_name" in
			*Quadro*|*Tesla*|*"RTX A"*|*" A"[0-9]0|*" A"[0-9]00|*" L"[0-9]|*" T"[0-9])
				ok "professional board — NVIDIA does not cap concurrent NVENC sessions here" ;;
			*GeForce*|*GTX*|*TITAN*|*Titan*)
				note_warning "consumer GeForce — NVIDIA caps CONCURRENT NVENC sessions on these
       boards (historically 2, raised to 3 and later 5 by driver releases).
       Confirm the number for driver $driver in NVIDIA's Video Encode and
       Decode GPU Support Matrix; channels past the cap fall back to libx264
       automatically and report the substitution, so budget CPU for them." ;;
			*)
				note_warning "unrecognized board '$gpu_name' — check NVIDIA's support matrix for
       its concurrent NVENC session limit before planning around it." ;;
		esac
	done < <(nvidia-smi --query-gpu=name,driver_version,encoder.stats.sessionCount \
		--format=csv,noheader 2>/dev/null || true)

	if (( NVENC_REQUESTED > 0 )); then
		log "$NVENC_REQUESTED channel(s) request h264_nvenc"
	else
		log "no channel requests h264_nvenc — the GPU is idle as far as this stack goes"
	fi

	docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia \
		&& ok "the nvidia container runtime is registered — channels get --gpus all" \
		|| note_warning "no nvidia runtime in 'docker info'. NVENC works on the host and NOT in
       a container without nvidia-container-toolkit. The container is what runs."

	log "the 360p HLS preview always encodes with libx264, so NVENC removes the"
	log "program encode from the CPU budget but never the preview encode."
fi

# ---------------------------------------------------------------------------
# 4. Uplink
# ---------------------------------------------------------------------------
head1 "4/4  uplink"

ok "$TOTAL_MBPS Mbps sustained upstream to YouTube for $CONFIGURED channel(s)"
log "CBR means sustained, not peak. Only the YouTube leg leaves the host —"
log "composer to relay and the HLS preview are internal. A saturated uplink"
log "looks exactly like a starving encoder from YouTube's side."

if (( HAS_4K )); then
	note_warning "a 2160p channel is configured. YouTube asks roughly 51-68 Mbps for 4K60;
       eight of those is about half a gigabit sustained, which is not a
       bandwidth a home connection has. Confirm the measured upstream of this
       link before going live, not the number on the bill."
else
	log "for reference: 4K60 wants ~51-68 Mbps EACH. Eight of those is ~0.5 Gbps"
	log "sustained — size high-resolution channels against the real uplink first."
fi

# ---------------------------------------------------------------------------
head1 "verdict"

if (( FAILED )); then
	die "this host is over budget. Nothing was changed."
fi
if (( WARNINGS > 0 )); then
	warn "$WARNINGS warning(s) above. None of them block a start; all are worth reading."
fi
ok "within budget"
