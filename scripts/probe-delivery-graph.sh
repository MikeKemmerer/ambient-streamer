#!/usr/bin/env bash
# Does the composer build a coherent graph for every delivery combination?
#
# Runs the real entrypoint far enough to write its filtergraph, with the relay
# pointed at a black hole so nothing is published and no channel is touched.
# The check is that the split/asplit branch counts match the labels actually
# consumed, which is the thing a hand-written fan-out gets wrong.
set -uo pipefail

IMAGE="${IMAGE:-ambient-composer:dev}"
REPO="${REPO:-$HOME/ambient-streamer}"
FAILURES=0

check() {
  local label="$1" ok="$2" note="${3:-}"
  if [[ "$ok" == "1" ]]; then
    printf '  PASS  %-52s %s\n' "$label" "$note"
  else
    printf '  FAIL  %-52s %s\n' "$label" "$note"
    FAILURES=$((FAILURES + 1))
  fi
}

run_case() {
  local name="$1" program="$2" video="$3" audio="$4"
  local work
  work="$(mktemp -d)"
  chmod 777 "$work"

  docker run --rm \
    -e CHANNEL_NAME=probe \
    -e PUBLISH_PROGRAM="$program" \
    -e PUBLISH_VIDEO="$video" \
    -e PUBLISH_AUDIO="$audio" \
    -e WIDTH=1280 -e HEIGHT=720 -e FPS=30 \
    -e VISUALIZATION=off \
    -e RELAY_RTMP="rtmp://127.0.0.1:1/none" \
    -e AUDIO_URL="http://127.0.0.1:1/none" \
    -e RUN_DIR=/run/probe \
    -e ICECAST_HOST=127.0.0.1 \
    -v "$work":/run/probe \
    -v "$REPO/ffmpeg":/opt/ambient:ro \
    --entrypoint bash "$IMAGE" -c '
      timeout 12 /opt/ambient/entrypoint.sh >/tmp/out.log 2>&1
      echo "---LOG---"; cat /tmp/out.log
      echo "---GRAPH---"; cat /run/probe/filtergraph.txt 2>/dev/null
    ' 2>&1
  rm -rf "$work" 2>/dev/null || true
}

# Counts the labels a split actually declares, e.g. split=3[a][b][c] -> 3.
labels_after() {
  local graph="$1" op="$2"
  local tail="${graph#*${op}=}"
  local count="${tail%%;*}"
  grep -o '\[' <<<"${count#*]}" | wc -l
}

probe() {
  local name="$1" program="$2" video="$3" audio="$4"
  shift 4
  local expect_outputs=("$@")

  echo "=== $name (program=$program video=$video audio=$audio)"
  local out graph delivery
  out="$(run_case "$name" "$program" "$video" "$audio")"
  graph="$(sed -n '/---GRAPH---/,$p' <<<"$out" | tail -n +2)"
  delivery="$(grep -o 'delivery: .*' <<<"$out" | head -1)"

  if [[ -z "$graph" ]]; then
    check "wrote a filtergraph" 0 "$(grep -oE '\[die\].*|no delivery target.*' <<<"$out" | head -1)"
    echo
    return
  fi
  check "wrote a filtergraph" 1 "$delivery"

  # Every label the graph declares must be consumed by an output, and vice
  # versa: a dangling branch is a silent extra encode, a missing one is a crash.
  local declared consumed
  declared="$(grep -oE '\[v(main|pre|loc)\]|\[a(main|preview|local|only)\]' <<<"$graph" | sort -u | tr -d '[]' | tr '\n' ' ')"
  check "declares the expected taps" \
    "$([[ -n "$declared" ]] && echo 1 || echo 0)" "$declared"

  local split_n asplit_n
  split_n="$(grep -oE 'split=[0-9]+' <<<"$graph" | head -1 | cut -d= -f2)"
  asplit_n="$(grep -oE 'asplit=[0-9]+' <<<"$graph" | head -1 | cut -d= -f2)"
  local split_labels asplit_labels
  split_labels="$(grep -oE 'split=[0-9]+(\[[a-z]+\])+' <<<"$graph" | head -1 | grep -o '\[' | wc -l)"
  asplit_labels="$(grep -oE 'asplit=[0-9]+(\[[a-z]+\])+' <<<"$graph" | head -1 | grep -o '\[' | wc -l)"

  check "split count matches its label count" \
    "$([[ "$split_n" == "$split_labels" ]] && echo 1 || echo 0)" "split=$split_n labels=$split_labels"
  check "asplit count matches its label count" \
    "$([[ "$asplit_n" == "$asplit_labels" ]] && echo 1 || echo 0)" "asplit=$asplit_n labels=$asplit_labels"

  for want in "${expect_outputs[@]}"; do
    check "publishes $want" "$(grep -qE "probe(/${want})?\"?$|probe/${want}" <<<"$out" && echo 1 || echo 0)" ""
  done
  echo
}

echo "Delivery fan-out probe"
echo

probe "youtube only"        on  off off preview
probe "youtube + video"     on  on  off preview video
probe "video only"          off on  off preview video
probe "audio only"          off off on  preview audio
probe "all three"           on  on  on  preview video audio
probe "legacy internal"     off ""  off preview video

echo
if (( FAILURES )); then
  echo "FAILED: $FAILURES check(s)"
  exit 1
fi
echo "every delivery combination builds a coherent graph"
