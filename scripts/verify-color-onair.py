#!/usr/bin/env python3
"""Capture real frames off the relay and report their average color.

Every earlier "the color works" conclusion came from a command reply, and FFmpeg
answered `0 Success` to commands that changed nothing. Only pixels settle it.
Frames are pulled from the RTMP live edge rather than HLS, because HLS serves
buffered segments and will happily hand back pre-change frames.
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


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def set_color(mode: str, accent: str = "", tint: str = "") -> int:
    payload: dict = {"mode": mode}
    if accent:
        payload["manual"] = {"accent": accent, "tint": tint or "#101820"}
    request = urllib.request.Request(
        f"http://127.0.0.1:8090/api/channels/{CHANNEL}/color",
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def grab(tag: str) -> tuple[int, int, int]:
    """Pull one frame from the relay using the composer's own ffmpeg."""
    out = f"/run/ambient/{CHANNEL}/probe-{tag}.png"
    subprocess.run(
        ["docker", "exec", composer(), "ffmpeg", "-v", "error", "-y",
         "-i", f"rtmp://mediamtx:1935/{CHANNEL}",
         "-frames:v", "1", "-update", "1", out],
        capture_output=True, timeout=90,
    )
    local = RUN / f"probe-{tag}.png"
    if not local.exists():
        return (-1, -1, -1)
    probe = subprocess.run(
        ["docker", "exec", composer(), "ffprobe", "-v", "error", "-f", "lavfi",
         f"movie={out},signalstats", "-show_entries",
         "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.UAVG,lavfi.signalstats.VAVG",
         "-of", "csv=p=0", "-read_intervals", "%+#1"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not probe:
        return (-1, -1, -1)
    y, u, v = (float(x) for x in probe[0].split(","))
    r = y + 1.402 * (v - 128)
    g = y - 0.344136 * (u - 128) - 0.714136 * (v - 128)
    b = y + 1.772 * (u - 128)
    return tuple(max(0, min(255, round(c))) for c in (r, g, b))  # type: ignore


def composer() -> str:
    names = subprocess.run(
        ["docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    return names[0] if names else ""


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<38} {note}")
        if not ok:
            failures.append(label)

    print(f"the picture must follow the operator's color ({composer()})")

    set_color("manual", "#FF3B30", "#2A0805")   # red
    time.sleep(12)
    red = grab("red")
    check("captured a frame", red != (-1, -1, -1), f"rgb{red}")
    check("red accent reads red-dominant", red[0] > red[1] and red[0] > red[2], f"rgb{red}")

    set_color("manual", "#34C759", "#052A0D")   # green
    time.sleep(12)
    green = grab("green")
    check("green accent reads green-dominant", green[1] >= green[0] and green[1] >= green[2], f"rgb{green}")

    moved = sum(abs(a - b) for a, b in zip(red, green))
    check("the two colors differ", moved > 30, f"delta {moved}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the operator's color reaches the picture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
