#!/usr/bin/env python3
"""Does grading the visualization alone actually change what you see?

The composite grade cannot express "make it green": hue@hue rotates, so the
result depends entirely on the artwork it starts from, and this channel's
artwork is magenta. The visualization is the element that should carry the
accent. This checks whether hue@viz can drive it live.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

CHANNEL = "vibecoding"
RUN = Path.home() / "ambient-streamer" / ".run" / CHANNEL

SENDER = (
    "import sys,zmq\n"
    "c=zmq.Context();s=c.socket(zmq.REQ)\n"
    "s.setsockopt(zmq.RCVTIMEO,5000);s.setsockopt(zmq.SNDTIMEO,5000)\n"
    "s.connect(sys.argv[1]);s.send_string(sys.argv[2])\n"
    "print(s.recv().decode(errors='replace'))\n"
)


def composer() -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    return out[0] if out else ""


def send(message: str) -> str:
    result = subprocess.run(
        ["docker", "exec", composer(), "python3", "-c", SENDER,
         "tcp://127.0.0.1:5555", message],
        capture_output=True, text=True, timeout=40,
    )
    return (result.stdout or result.stderr).strip()


def grab(tag: str) -> tuple[int, int, int]:
    out = f"/run/ambient/{CHANNEL}/viz-{tag}.png"
    subprocess.run(
        ["docker", "exec", composer(), "ffmpeg", "-v", "error", "-y",
         "-i", f"rtmp://mediamtx:1935/{CHANNEL}", "-frames:v", "1", "-update", "1", out],
        capture_output=True, timeout=90,
    )
    if not (RUN / f"viz-{tag}.png").exists():
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


def main() -> int:
    print(f"can the visualization be graded live? ({composer()})")
    print(f"  hue@viz exists      : {send('hue@viz h 0')}")
    base = grab("base")
    print(f"  baseline            : rgb{base}")

    for degrees in (90, 180, 270):
        reply = send(f"hue@viz h {degrees}")
        time.sleep(8)
        got = grab(f"h{degrees}")
        delta = sum(abs(a - b) for a, b in zip(base, got)) if got != (-1, -1, -1) else -1
        print(f"  hue@viz h {degrees:<4}      : rgb{got}  delta {delta}   [{reply}]")

    print(f"  saturation 0        : {send('hue@viz s 0')}")
    time.sleep(8)
    print(f"  desaturated viz     : rgb{grab('s0')}")

    send("hue@viz h 0")
    send("hue@viz s 1")
    print("  restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
