#!/usr/bin/env python3
"""Generate synthetic slideshow test images.

Each image is a flat, unmistakable primary colour with a large index label so
frame inspection can identify which slide (or blend of two) is on screen.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

# Far apart in RGB so a blend ratio is recoverable from a sampled pixel.
COLORS = [
    (220, 20, 20), (20, 200, 20), (20, 20, 220), (220, 200, 20),
    (200, 20, 200), (20, 200, 200), (240, 120, 20), (120, 20, 240),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--prefix", default="img")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    for i in range(args.start, args.start + args.count):
        color = COLORS[i % len(COLORS)]
        img = Image.new("RGB", (args.width, args.height), color)
        d = ImageDraw.Draw(img)
        # Corner blocks give frameprobe stable sample regions independent of text.
        d.rectangle([0, 0, args.width // 8, args.height // 8], fill=(255, 255, 255))
        d.text((args.width // 2, args.height // 2), f"{args.prefix}{i:02d}",
               fill=(255, 255, 255))
        path = os.path.join(args.dir, f"{args.prefix}{i:02d}.png")
        img.save(path)
        print(f"{path} {color}")


if __name__ == "__main__":
    main()
