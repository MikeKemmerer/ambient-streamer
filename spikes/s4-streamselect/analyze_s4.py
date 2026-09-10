#!/usr/bin/env python3
"""Analyse an S4 recording: prove from pixels which branch reached the output."""

from __future__ import annotations

import argparse
import json

from frameprobe import Region, fmt, sample

FPS = 30.0

# Quadrant means give a per-frame fingerprint as well as a dominant colour.
QUADS = [
    Region("tl", 160, 90, 0, 0),
    Region("tr", 160, 90, 160, 0),
    Region("bl", 160, 90, 0, 90),
    Region("br", 160, 90, 160, 90),
]

BRANCH = {0: "0 showwaves/red", 1: "1 showfreqs/green", 2: "2 avectorscope/blue"}


def totals(frame) -> tuple[int, int, int]:
    return tuple(sum(px[c] for px in frame) for c in range(3))


def classify(frame) -> str:
    r, g, b = totals(frame)
    if max(r, g, b) < 8:
        return "black"
    order = sorted(((r, 0), (g, 1), (b, 2)), reverse=True)
    if order[0][0] - order[1][0] < 3:
        return "ambiguous"
    return BRANCH[order[0][1]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("commands")
    ap.add_argument("--duration", type=float, default=0.0)
    args = ap.parse_args()

    frames = sample(args.video, QUADS)
    with open(args.commands, encoding="utf-8") as fh:
        rows = json.load(fh)

    classes = [classify(f) for f in frames]
    dupes = [i for i in range(1, len(frames)) if frames[i] == frames[i - 1]]
    blacks = [i for i, c in enumerate(classes) if c == "black"]
    ambiguous = [i for i, c in enumerate(classes) if c == "ambiguous"]

    print(f"frames decoded        : {len(frames)}")
    if args.duration:
        print(f"frames expected       : {int(args.duration * FPS)} "
              f"({args.duration}s at {FPS} fps)")
    print(f"black frames          : {len(blacks)} {blacks[:12]}")
    print(f"ambiguous frames      : {len(ambiguous)} {ambiguous[:12]}")
    print(f"exact duplicate frames: {len(dupes)} {dupes[:12]}")

    transitions = [i for i in range(1, len(classes)) if classes[i] != classes[i - 1]]
    print(f"class transitions     : {len(transitions)} at frames {transitions}")

    if not transitions:
        print("\nNO BRANCH CHANGE DETECTED -- spike FAILS")
        return

    f0 = None
    print("\ncommand correlation (expected frame = f0 + t_rel * fps)")
    print("  t_rel     expected  observed  err  branch after         command")
    real = [t for t in transitions]
    # command 0 is the t0 marker and changes nothing, so anchor on command 1.
    if len(rows) > 1 and real:
        f0 = real[0] - round(rows[1]["t_rel"] * FPS)
    for i, row in enumerate(rows):
        if i == 0:
            print(f"  {row['t_rel']:7.3f}  {'-':>8s}  {'-':>8s}  {'-':>4s}  "
                  f"{'(t0 marker, no change)':20s}  {row['msg']}")
            continue
        expected = f0 + round(row["t_rel"] * FPS)
        obs = real[i - 1] if i - 1 < len(real) else None
        err = "n/a" if obs is None else f"{obs - expected:+d}"
        after = classes[obs] if obs is not None else "MISSING"
        print(f"  {row['t_rel']:7.3f}  {expected:8d}  "
              f"{'MISSING' if obs is None else obs:>8}  {err:>4s}  {after:20s}  {row['msg']}")

    print("\nframe-by-frame around each transition (quadrant means, tl tr bl br)")
    for t in transitions:
        print(f"  -- transition at frame {t} --")
        for i in range(max(0, t - 3), min(len(frames), t + 4)):
            marker = ">>" if i == t else "  "
            px = " ".join(fmt(p) for p in frames[i])
            dup = " DUP" if i in dupes else ""
            print(f"  {marker} {i:5d}  {px}  {classes[i]}{dup}")


if __name__ == "__main__":
    main()
