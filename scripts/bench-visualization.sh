#!/usr/bin/env bash
# What actually makes the visualization cheaper?
#
# Measured first: the visualization is ~91% of a channel's CPU (0.05 cores
# without it, 0.94 with it). showfreqs and showwaves are audio-to-video
# synthesis filters and have no CUDA equivalent, so the GPU cannot take this
# work. These are the levers that remain.
#
# Paced with `realtime` so a reading is cores per second of output, not however
# fast the host will let ffmpeg run.
set -uo pipefail

IMAGE="${AMBIENT_COMPOSER_IMAGE:-ambient-composer:dev}"
WINDOW="${WINDOW:-25}"
NAME=ambient-vizbench

ENC=(-c:v h264_nvenc -preset p4 -tune ll -rc cbr -cbr 1
     -b:v 4500k -maxrate 4500k -minrate 4500k -bufsize 9000k -g 60 -pix_fmt yuv420p)

ALPHA='split=2[vc][vm];[vm]format=gray,lut=y=val*0.65[va];[vc][va]alphamerge[vr];[base][vr]overlay=eof_action=pass:format=auto'
GRADE='eq=eval=frame:contrast=1,hue=h=0,format=yuv420p,setsar=1'
BASE='[1:v]fps=30,realtime,format=yuv420p,setsar=1'

run() {
	local label="$1" graph="$2"
	docker rm -f "$NAME" >/dev/null 2>&1
	docker run -d --name "$NAME" --gpus all \
		-e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
		--entrypoint ffmpeg "$IMAGE" -hide_banner -nostdin -v error \
		-f lavfi -i "anoisesrc=color=pink:sample_rate=44100:duration=200" \
		-f lavfi -i "testsrc2=size=1280x720:rate=10:duration=200" \
		-filter_complex "$graph" -map '[out]' "${ENC[@]}" \
		-t 180 -f null - >/dev/null 2>&1
	sleep 10
	local a b
	a=$(docker exec "$NAME" awk '/usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
	sleep "$WINDOW"
	b=$(docker exec "$NAME" awk '/usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
	local logs; logs=$(docker logs "$NAME" 2>&1 | tail -1)
	docker rm -f "$NAME" >/dev/null 2>&1
	if [[ -z "$a" || -z "$b" ]]; then
		printf '  %-46s FAILED  %s\n' "$label" "${logs:0:90}"
		return
	fi
	awk -v a="$a" -v b="$b" -v l="$label" -v w="$WINDOW" \
		'BEGIN{printf "  %-46s %.2f cores\n", l, (b-a)/1e6/w}'
}

freqs() { # size fps win_size
	printf '[0:a]showfreqs@viz=s=%s:mode=bar:ascale=log:fscale=log:win_size=%s:averaging=2:colors=0x34C759|0x34C759,fps=%s,format=yuv420p,setsar=1' "$1" "$3" "$2"
}

echo "visualization levers, paced at 1x, ${WINDOW}s window, encode on GPU"
echo

run "a. today: 1280x720 @30, win 1024" \
	"${BASE}[base];$(freqs 1280x720 30 1024)[v];[v]${ALPHA},${GRADE}[out]"

run "b. rendered @15, frames duplicated to 30" \
	"${BASE}[base];$(freqs 1280x720 15 1024),fps=30[v];[v]${ALPHA},${GRADE}[out]"

run "c. 640x360 @30, upscaled" \
	"${BASE}[base];$(freqs 640x360 30 1024),scale=1280:720:flags=fast_bilinear[v];[v]${ALPHA},${GRADE}[out]"

run "d. 640x360 @15, upscaled and duplicated" \
	"${BASE}[base];$(freqs 640x360 15 1024),fps=30,scale=1280:720:flags=fast_bilinear[v];[v]${ALPHA},${GRADE}[out]"

run "e. 1280x720 @30, win 512" \
	"${BASE}[base];$(freqs 1280x720 30 512)[v];[v]${ALPHA},${GRADE}[out]"

run "f. showwaves instead of showfreqs" \
	"${BASE}[base];[0:a]showwaves@viz=s=1280x720:mode=line:rate=30:draw=scale:scale=sqrt:split_channels=0:colors=0x34C759|0x34C759,fps=30,format=yuv420p,setsar=1[v];[v]${ALPHA},${GRADE}[out]"

echo
echo "  the no-visualization floor is 0.05 cores, so anything above that is the"
echo "  visualization and the compositing it feeds"
