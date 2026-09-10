#!/usr/bin/env python3
"""Measure what a colour change actually does to the picture.

The operator reports colour "does nothing". The filtergraph grades only the
slideshow layer and blends the visualisation in afterwards, so the prediction is
that the background moves and the bright overlay does not. Frames are captured
from inside the Docker network (the HLS port is not published to the host) and
written to the shared run directory so the host can read them.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
CHANNEL = "vibecoding"
RUN = ROOT / ".run" / CHANNEL
HLS = f"http://mediamtx:8888/{CHANNEL}/preview/index.m3u8"


def composer() -> str:
    names = subprocess.run(
        ["docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    return names[0] if names else ""


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def grab(box: str, tag: str) -> Path | None:
    inside = f"/run/ambient/{CHANNEL}/probe-{tag}.png"
    result = subprocess.run(
        ["docker", "exec", box, "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-i", HLS, "-frames:v", "1", "-update", "1", inside],
        capture_output=True, text=True, timeout=120,
    )
    out = RUN / f"probe-{tag}.png"
    if not out.exists():
        print(f"    capture failed: {result.stderr.strip()[:200]}")
        return None
    return out


def layers(image: Path) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Average colour of the dark background and of the bright overlay.

    The visualisation is screen-blended, so it is simply the bright pixels.
    """
    from PIL import Image

    pixels = list(Image.open(image).convert("RGB").resize((160, 90)).getdata())
    dark = [p for p in pixels if sum(p) < 240]
    bright = [p for p in pixels if sum(p) >= 240]

    def mean(group):
        if not group:
            return (0.0, 0.0, 0.0)
        n = len(group)
        return tuple(round(sum(p[i] for p in group) / n, 1) for i in range(3))

    return mean(dark), mean(bright)


def set_color(accent: str, tint: str) -> None:
    body = json.dumps({"mode": "manual", "manual": {"accent": accent, "tint": tint}}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:8090/api/channels/{CHANNEL}/color",
        data=body, method="PUT",
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=60).read()


def main() -> int:
    box = composer()
    print(f"what does a colour change actually move? ({box})")

    samples = {}
    for tag, accent, tint in (("magenta", "#FF00FF", "#200020"), ("green", "#00FF00", "#002000")):
        print(f"  setting {tag} ...")
        set_color(accent, tint)
        time.sleep(14)
        frame = grab(box, tag)
        if frame is None:
            return 1
        dark, bright = layers(frame)
        samples[tag] = (dark, bright)
        print(f"    background {dark}   overlay {bright}")

    (d1, b1), (d2, b2) = samples["magenta"], samples["green"]
    bg_move = sum(abs(a - b) for a, b in zip(d1, d2))
    fg_move = sum(abs(a - b) for a, b in zip(b1, b2))

    print()
    print(f"  background moved {bg_move:.1f}, overlay moved {fg_move:.1f} (sum of RGB deltas)")
    if bg_move < 6 and fg_move < 6:
        print("  -> colour is not reaching the picture at all")
    elif fg_move < 6:
        print("  -> colour grades the background only; the visualisation ignores it")
    else:
        print("  -> colour reaches the whole picture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
