#!/usr/bin/env python3
"""Inspect a recorded slideshow: slide colours, transition length, blend steps."""

from __future__ import annotations

import argparse
import subprocess
import sys

sys.path.insert(0, __file__.rsplit("/", 3)[0] + "/lib")


def sample_center(path: str) -> list[tuple[int, int, int]]:
    """One mean RGB per frame from the middle band, avoiding the corner marker."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
        "-vf", "crop=iw/2:ih/2:iw/4:ih/4,scale=1:1:flags=area,setsar=1,format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    raw = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, check=True).stdout
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 3)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    f = sample_center(args.path)
    if not f:
        print("ANALYZE: no frames decoded")
        raise SystemExit(1)

    deltas = [max(abs(a - b) for a, b in zip(f[i], f[i - 1])) for i in range(1, len(f))]
    moving = [i for i, d in enumerate(deltas, 1) if d > 2]
    static = len(deltas) - len(moving)

    # A crossfade is a burst of steps; upsampling holds each step for several
    # output frames, so bridge gaps shorter than half a second.
    gap = int(args.fps // 2)
    runs = []
    for i in moving:
        if runs and i - runs[-1][1] <= gap:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    runs = [(a, b) for a, b in runs]

    uniq = {}
    for c in f:
        key = tuple(v // 24 for v in c)
        uniq[key] = uniq.get(key, 0) + 1

    print(f"ANALYZE {args.path}")
    print(f"  frames={len(f)}  duration={len(f)/args.fps:.1f}s  "
          f"distinct_colour_buckets={len(uniq)}")
    print(f"  static_frames={static}  changing_frames={len(moving)}  "
          f"transitions={len(runs)}")
    for a, b in runs[:6]:
        span = b - a + 1
        seg = [d for d in deltas[a - 1:b] if d > 2]
        print(f"    transition frames {a}-{b} ({span} frames, {span/args.fps:.2f}s) "
              f"steps={len(seg)} max_step={max(seg) if seg else 0}")
    if runs:
        steps = [deltas[i - 1] for a, b in runs for i in range(a, b + 1)
                 if deltas[i - 1] > 2]
        distinct = len({tuple(f[i]) for a, b in runs for i in range(a, b + 1)})
        print(f"  blend: distinct_intermediate_frames={distinct} "
              f"steps_per_transition={len(steps)/len(runs):.1f} "
              f"max_step={max(steps)} mean_step={sum(steps)/len(steps):.1f}")
    print(f"  first={f[0]} mid={f[len(f)//2]} last={f[-1]}")


if __name__ == "__main__":
    main()
