#!/usr/bin/env python3
"""Report what a streamselect graph with a mismatched branch actually produced."""

from __future__ import annotations

import argparse
import json
import subprocess

from frameprobe import Region, fmt, sample


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,pix_fmt,avg_frame_rate,sample_aspect_ratio,nb_frames",
         "-of", "json", path],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, check=True,
    ).stdout
    return json.loads(out)["streams"][0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("commands")
    args = ap.parse_args()

    s = probe(args.video)
    print(f"  output stream : {s['width']}x{s['height']} {s['pix_fmt']} "
          f"sar={s.get('sample_aspect_ratio')} fps={s.get('avg_frame_rate')} "
          f"nb_frames={s.get('nb_frames')}")

    with open(args.commands, encoding="utf-8") as fh:
        rows = json.load(fh)
    for r in rows:
        print(f"  t+{r['t_rel']:6.3f}s  reply={r['reply']!r}  {r['msg']}")

    frames = sample(args.video, [Region("frame", int(s["width"]), int(s["height"]), 0, 0)])
    # A whole-frame mean cannot distinguish a black frame from thin bright bars on
    # black; phase J's signalstats output is the authority on that.
    classes = []
    for f in frames:
        r, g, b = f[0]
        classes.append("near-zero mean" if max(r, g, b) < 3
                       else ("red/branch0" if r >= g and r >= b
                             else ("green/branch1" if g >= b else "blue")))
    transitions = [i for i in range(1, len(classes)) if classes[i] != classes[i - 1]]
    print(f"  frames decoded: {len(frames)}  transitions at {transitions}")
    for i in transitions:
        print(f"    frame {i - 1:4d} {fmt(frames[i - 1][0])} {classes[i - 1]:14s}"
              f" -> frame {i:4d} {fmt(frames[i][0])} {classes[i]}")
    if not transitions:
        print("    NO BRANCH CHANGE REACHED THE OUTPUT")


if __name__ == "__main__":
    main()
