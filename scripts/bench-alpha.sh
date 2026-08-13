#!/usr/bin/env bash
# The alpha composite costs 0.32 of a channel's 0.94 cores. Can that be bought
# back without giving up the accent colour?
#
# The composite has to be alpha, not additive: an additive chroma blend can only
# shift the artwork's colour, so green bars over orange artwork came out orange.
# The question is only how cheaply the alpha is derived from the visualization's
# own luma.
set -uo pipefail

IMAGE="${AMBIENT_COMPOSER_IMAGE:-ambient-composer:dev}"
WINDOW="${WINDOW:-25}"
NAME=ambient-alpha

GRADE='eq=eval=frame:contrast=1,hue=h=0,format=yuv420p,setsar=1'
BASE='[1:v]fps=30,realtime,format=yuv420p,setsar=1'
VIZ='[0:a]showfreqs@viz=s=1280x720:mode=bar:ascale=log:fscale=log:win_size=1024:averaging=2:colors=0x34C759|0x34C759,fps=30'

run() {
	local label="$1" graph="$2"
	docker rm -f "$NAME" >/dev/null 2>&1
	docker run -d --name "$NAME" --gpus all \
		-e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
		--entrypoint ffmpeg "$IMAGE" -hide_banner -nostdin -v error \
		-f lavfi -i "anoisesrc=color=pink:sample_rate=44100:duration=200" \
		-f lavfi -i "testsrc2=size=1280x720:rate=10:duration=200" \
		-filter_complex "$graph" -map '[out]' \
		-c:v h264_nvenc -preset p4 -tune ll -rc cbr -cbr 1 \
		-b:v 4500k -maxrate 4500k -minrate 4500k -bufsize 9000k -g 60 -pix_fmt yuv420p \
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

echo "ways to composite the visualization, paced at 1x, ${WINDOW}s window"
echo

run "0. plain overlay, no alpha (wrong look, floor)" \
	"${BASE}[base];${VIZ},format=yuv420p,setsar=1[v];[base][v]overlay=format=auto,${GRADE}[out]"

run "1. today: split+gray+lut+alphamerge" \
	"${BASE}[base];${VIZ},format=yuv420p,setsar=1,split=2[vc][vm];[vm]format=gray,lut=y=val*0.65[va];[vc][va]alphamerge[vr];[base][vr]overlay=eof_action=pass:format=auto,${GRADE}[out]"

run "2. lumakey + alpha scale" \
	"${BASE}[base];${VIZ},format=yuva420p,lumakey=threshold=0:tolerance=0.02:softness=0.15,colorchannelmixer=aa=0.65[vr];[base][vr]overlay=eof_action=pass:format=auto,${GRADE}[out]"

run "3. lumakey, no separate alpha scale" \
	"${BASE}[base];${VIZ},format=yuva420p,lumakey=threshold=0:tolerance=0.02:softness=0.15[vr];[base][vr]overlay=eof_action=pass:format=auto,${GRADE}[out]"

run "4. geq alpha from luma" \
	"${BASE}[base];${VIZ},format=yuva420p,geq=lum='p(X,Y)':a='0.65*lum(X,Y)'[vr];[base][vr]overlay=eof_action=pass:format=auto,${GRADE}[out]"

run "5. today's chain but plugin already yuva420p" \
	"${BASE}[base];${VIZ},format=yuva420p,setsar=1,split=2[vc][vm];[vm]format=gray,lut=y=val*0.65[va];[vc][va]alphamerge[vr];[base][vr]overlay=eof_action=pass:format=auto,${GRADE}[out]"

echo
echo "  0 is the cost with no alpha at all; 1 is what runs today"
