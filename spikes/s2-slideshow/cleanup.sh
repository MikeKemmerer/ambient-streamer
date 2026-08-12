#!/usr/bin/env bash
# Standalone teardown: kills anything this spike could have left behind.
set -uo pipefail
echo "== before =="
pgrep -af 's2spike-t[0-9]|producer\.py' || echo "  (nothing)"
pkill -f 's2spike-t[0-9]' 2>/dev/null
pkill -f 'producer\.py' 2>/dev/null
sleep 1
pkill -9 -f 's2spike-t[0-9]' 2>/dev/null
pkill -9 -f 'producer\.py' 2>/dev/null
sleep 0.5
echo "== after =="
pgrep -af 's2spike-t[0-9]|producer\.py' && echo "  STILL RUNNING" || echo "  clean"
echo "== any ffmpeg at all =="
pgrep -af ffmpeg || echo "  none"
exit 0
