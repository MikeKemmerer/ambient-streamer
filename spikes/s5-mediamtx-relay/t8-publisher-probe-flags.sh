#!/bin/bash
# Which probe flags reliably recover video parameters from the relay?
set -u
IMG=ambient-mediamtx:dev

run_variant() {
  local label="$1"; shift
  local ok=0 fail=0 i out warn
  for i in 1 2 3; do
    docker rm -f ambient-ft >/dev/null 2>&1
    docker run --rm -d --name ambient-ft --network ambient --entrypoint ffmpeg "$IMG" \
      -nostdin -hide_banner -loglevel warning "$@" \
      -i rtmp://mediamtx:1935/vibecoding -c copy -f flv -flvflags no_duration_filesize \
      -rtmp_app fttest -rtmp_playpath preview rtmp://mediamtx:1935 >/dev/null
    sleep 6
    out=$(docker run --rm --network ambient --entrypoint ffprobe "$IMG" -hide_banner -v error \
      -show_entries stream=codec_name,width,height -of csv=p=0 \
      rtmp://mediamtx:1935/fttest/preview 2>&1 | tr '\n' ' ')
    warn=$(docker logs ambient-ft 2>&1 | grep -c 'Could not find codec parameters')
    docker rm -f ambient-ft >/dev/null 2>&1
    if [[ "$out" == *"1280,720"* && "$warn" == "0" ]]; then ok=$((ok+1)); else fail=$((fail+1)); fi
    printf '    run%d: probe=[%s] codecwarn=%s\n' "$i" "${out% }" "$warn"
  done
  printf '  RESULT %-52s ok=%d fail=%d\n\n' "$label" "$ok" "$fail"
}

echo "=== A: nobuffer + fast probe (as specified) ==="
run_variant "-fflags nobuffer -analyzeduration 500000 -probesize 250000" \
  -fflags nobuffer -analyzeduration 500000 -probesize 250000

echo "=== B: fast probe, no nobuffer ==="
run_variant "-analyzeduration 500000 -probesize 250000" \
  -analyzeduration 500000 -probesize 250000

echo "=== C: nobuffer + one-GOP analyzeduration ==="
run_variant "-fflags nobuffer -analyzeduration 2500000 -probesize 1000000" \
  -fflags nobuffer -analyzeduration 2500000 -probesize 1000000

docker rm -f ambient-ft >/dev/null 2>&1
echo "done"
