#!/usr/bin/env python3
"""Does a runtime colour command survive long enough to reach the picture?

Commands reply "0 Success" but the picture does not move. Either they are not
applied, or something resets the graph. A composer restart reinitialises every
filter to its launch defaults, so uptime is recorded either side of the test.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

CHANNEL = "vibecoding"
RUN = Path.home() / "ambient-streamer" / ".run" / CHANNEL


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def box() -> str:
    return sh("docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}").split()[0]


def started(container: str) -> str:
    return sh("docker", "inspect", "-f", "{{.State.StartedAt}}", container)


def send(container: str, message: str) -> str:
    code = (
        "import zmq;c=zmq.Context();s=c.socket(zmq.REQ);s.setsockopt(zmq.RCVTIMEO,8000);"
        "s.connect('tcp://127.0.0.1:5555');s.send_string(%r);print(s.recv_string())" % message
    )
    return sh("docker", "exec", container, "python3", "-c", code)


def grab(container: str, tag: str) -> Path:
    subprocess.run(
        ["docker", "exec", container, "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
         "-i", f"rtmp://mediamtx:1935/{CHANNEL}", "-frames:v", "1", "-update", "1",
         f"/run/ambient/{CHANNEL}/probe-{tag}.png"],
        capture_output=True, timeout=120,
    )
    return RUN / f"probe-{tag}.png"


def saturation_of(image: Path) -> float:
    from PIL import Image

    pixels = Image.open(image).convert("HSV").resize((160, 90))
    data = list(pixels.getdata())
    return round(sum(p[1] for p in data) / len(data), 1)


def main() -> int:
    container = box()
    before_start = started(container)
    print(f"container {container}, started {before_start}")

    base = grab(container, "ctl-before")
    print(f"  saturation before      : {saturation_of(base)}")

    print(f"  send eq@eq saturation 0: {send(container, 'eq@eq saturation 0')}")
    shot = grab(container, "ctl-after")
    print(f"  saturation after       : {saturation_of(shot)}")

    print(f"  send eq@eq saturation 3: {send(container, 'eq@eq saturation 3')}")
    shot2 = grab(container, "ctl-max")
    print(f"  saturation at 3        : {saturation_of(shot2)}")

    after_start = started(container)
    print(f"  container restarted during test: {after_start != before_start}")

    send(container, "eq@eq saturation 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
