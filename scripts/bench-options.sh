#!/usr/bin/env bash
# The full option space for making a channel cheaper.
#
# Established already: the visualization is ~91% of a channel's CPU, and it
# cannot move to the GPU because showfreqs/showwaves synthesize video from audio
# and FFmpeg's CUDA filters are all transcode-oriented. So every option here is
# about doing less work, not moving it.
#
# Paced with `realtime`, so a reading is cores per second of stream.
set -uo pipefail

IMAGE="${AMBIENT_COMPOSER_IMAGE:-ambient-composer:dev}"
WINDOW="${WINDOW:-25}"
NAME=ambient-opts

ALPHA='split=2[vc][vm];[vm]format=gray,lut=y=val*0.65[va];[vc][va]alphamerge[vr];[base][vr]overlay=eof_action=pass:format=auto'
GRADE='eq=eval=frame:contrast=1,hue=h=0,format=yuv420p,setsar=1'

run() { # label  width height fps  graph
	local label="$1" w="$2" h="$3" fps="$4" graph="$5"
	local rate; rate=$(( w * h * fps / 27648 ))  # ~kbps by the project's ladder
	docker rm -f "$NAME" >/dev/null 2>&1
	docker run -d --name "$NAME" --gpus all \
		-e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
		--entrypoint ffmpeg "$IMAGE" -hide_banner -nostdin -v error \
		-f lavfi -i "anoisesrc=color=pink:sample_rate=44100:duration=200" \
		-f lavfi -i "testsrc2=size=${w}x${h}:rate=10:duration=200" \
		-filter_complex "$graph" -map '[out]' \
		-c:v h264_nvenc -preset p4 -tune ll -rc cbr -cbr 1 \
		-b:v "${rate}k" -maxrate "${rate}k" -minrate "${rate}k" \
		-bufsize "$((rate * 2))k" -g "$((fps * 2))" -pix_fmt yuv420p \
		-t 180 -f null - >/dev/null 2>&1
	sleep 10
	local a b
	a=$(docker exec "$NAME" awk '/usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
	sleep "$WINDOW"
	b=$(docker exec "$NAME" awk '/usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
	local logs; logs=$(docker logs "$NAME" 2>&1 | tail -1)
	docker rm -f "$NAME" >/dev/null 2>&1
	if [[ -z "$a" || -z "$b" ]]; then
		printf '  %-48s FAILED  %s\n' "$label" "${logs:0:80}"
		return
	fi
	awk -v a="$a" -v b="$b" -v l="$label" -v w="$WINDOW" \
		'BEGIN{printf "  %-48s %.2f cores\n", l, (b-a)/1e6/w}'
}

freqs() { # WxH fps
	printf '[0:a]showfreqs@viz=s=%s:mode=bar:ascale=log:fscale=log:win_size=1024:averaging=2:colors=0x34C759|0x34C759,fps=%s,format=yuv420p,setsar=1' "$1" "$2"
}
base() { printf '[1:v]fps=%s,realtime,format=yuv420p,setsar=1' "$1"; }

echo "channel cost options, paced at 1x, ${WINDOW}s window, encode on GPU"
echo

echo "  -- the floor --"
run "slides only, no visualization at all" 1280 720 30 \
	"$(base 30),${GRADE}[out]"

echo
echo "  -- today, and cheaper compositing --"
run "720p30 + visualization, alpha composite (today)" 1280 720 30 \
	"$(base 30)[base];$(freqs 1280x720 30)[v];[v]${ALPHA},${GRADE}[out]"
run "720p30, plain overlay instead of alpha" 1280 720 30 \
	"$(base 30)[base];$(freqs 1280x720 30)[v];[base][v]overlay=format=auto,${GRADE}[out]"
run "720p30, no grade filters at all" 1280 720 30 \
	"$(base 30)[base];$(freqs 1280x720 30)[v];[v]${ALPHA},format=yuv420p,setsar=1[out]"

echo
echo "  -- lower output rate --"
run "720p24 + visualization" 1280 720 24 \
	"$(base 24)[base];$(freqs 1280x720 24)[v];[v]${ALPHA},${GRADE}[out]"
run "720p20 + visualization" 1280 720 20 \
	"$(base 20)[base];$(freqs 1280x720 20)[v];[v]${ALPHA},${GRADE}[out]"

echo
echo "  -- lower output resolution (whole pipeline, no upscale) --"
run "480p30 + visualization" 854 480 30 \
	"$(base 30)[base];$(freqs 854x480 30)[v];[v]${ALPHA},${GRADE}[out]"
run "480p24 + visualization" 854 480 24 \
	"$(base 24)[base];$(freqs 854x480 24)[v];[v]${ALPHA},${GRADE}[out]"
run "360p30 + visualization" 640 360 30 \
	"$(base 30)[base];$(freqs 640x360 30)[v];[v]${ALPHA},${GRADE}[out]"

echo
echo "  -- visualization rendered slower than output --"
run "720p30, visualization drawn at 15 and held" 1280 720 30 \
	"$(base 30)[base];$(freqs 1280x720 15),fps=30[v];[v]${ALPHA},${GRADE}[out]"
run "720p30, visualization drawn at 10 and held" 1280 720 30 \
	"$(base 30)[base];$(freqs 1280x720 10),fps=30[v];[v]${ALPHA},${GRADE}[out]"
