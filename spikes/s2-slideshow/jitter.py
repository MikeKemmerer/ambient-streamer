#!/usr/bin/env python3
"""Encoding-rate jitter from an ffmpeg -progress file.

ffmpeg emits a progress block roughly every 0.5s. Comparing how much media time
advanced per block against wall clock exposes burst delivery: a steady stream
sits near 1.0x, a bursty one alternates high and low.
"""

from __future__ import annotations

import sys


def main() -> None:
    blocks, cur = [], {}
    with open(sys.argv[1]) as fh:
        for line in fh:
            k, _, v = line.strip().partition("=")
            cur[k] = v
            if k == "progress":
                blocks.append(cur)
                cur = {}
    times = []
    for b in blocks:
        try:
            times.append((int(b["out_time_us"]), int(b["frame"])))
        except (KeyError, ValueError):
            continue
    if len(times) < 4:
        print("    jitter: not enough progress blocks")
        return
    dframes = [times[i][1] - times[i - 1][1] for i in range(1, len(times))]
    dus = [times[i][0] - times[i - 1][0] for i in range(1, len(times))]
    dframes = [d for d in dframes if d >= 0]
    mean = sum(dframes) / len(dframes)
    spread = max(dframes) - min(dframes)
    print(f"    frames per progress block: min={min(dframes)} max={max(dframes)} "
          f"mean={mean:.1f} spread={spread}  "
          f"media_us per block mean={sum(dus)/len(dus):.0f}")


if __name__ == "__main__":
    main()
