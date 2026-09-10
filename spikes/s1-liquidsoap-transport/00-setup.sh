#!/usr/bin/env bash
# Pull the pinned Liquidsoap image and synthesize test audio.
# Left channel and right channel carry different tones so a post-reconnect
# channel swap or byte misalignment is detectable in the recording.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DUR="${DUR:-45}"

log "pulling $LIQ_IMAGE"
docker pull "$LIQ_IMAGE" >/dev/null
ok "image present"

mkdir -p "$MEDIA_DIR" "$SPIKE_DIR/hold"

make_tone() {
  local path="$1" fl="$2" fr="$3"
  [[ -f "$path" ]] && return 0
  ff -loglevel error \
    -f lavfi -i "sine=frequency=$fl:sample_rate=44100:duration=$DUR" \
    -f lavfi -i "sine=frequency=$fr:sample_rate=44100:duration=$DUR" \
    -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo,volume=-6dB[a]" \
    -map "[a]" -c:a libmp3lame -b:a 256k -ar 44100 -y "$path"
}

make_tone "$MEDIA_DIR/t1-a.mp3"  330  495
make_tone "$MEDIA_DIR/t2-b.mp3"  440  660
make_tone "$MEDIA_DIR/t3-c.mp3"  550  825
make_tone "$MEDIA_DIR/t4-d.mp3"  660  990
# staged outside MEDIA_DIR; 50-playlist-watch.sh copies it in mid-run
make_tone "$SPIKE_DIR/hold/t9-new.mp3" 1200 1800

ok "test media:"
ls -1 "$MEDIA_DIR" "$SPIKE_DIR/hold"
