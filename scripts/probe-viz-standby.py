#!/usr/bin/env python3
"""Can the visualization be switched on and off on a RUNNING graph?

The standby idea rests on `overlay` honouring an `enable` command over zmq.
Timeline support in `-h filter=overlay` is a claim about the build, not proof
that the command lands, so this drives a real graph and reads the pixels either
side of the switch.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

OUT = Path("/tmp/standby-probe")
PORT = 5599

GRAPH = (
    # `realtime` or the run finishes in a tenth of a second and the command
    # arrives after ffmpeg has already exited.
    "color=c=black:s=320x240:r=10,realtime,"
    # Two backslashes reach the file, matching what entrypoint.sh's printf emits.
    f"zmq@ctl=bind_address=tcp\\\\://127.0.0.1\\\\:{PORT},format=yuv420p[base];"
    "color=c=green:s=320x240:r=10,format=yuv420p[viz];"
    "[base][viz]overlay@viz=eof_action=pass:format=auto:enable=1[out]"
)


def centre_pixel(png: Path) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(png),
         "-vf", "crop=1:1:160:120", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True).stdout
    return tuple(raw[:3]) if len(raw) >= 3 else (-1, -1, -1)


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    script = OUT / "graph.txt"
    script.write_text(GRAPH, encoding="utf-8")

    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
         "-f", "lavfi", "-i", "nullsrc=s=32x32:r=10",
         "-filter_complex_script", str(script),
         "-map", "[out]", "-frames:v", "80", "-y", str(OUT / "frame-%03d.png")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(2.5)

    try:
        import zmq
    except ImportError:
        proc.kill()
        print("pyzmq missing in this image")
        return 2

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 4000)
    sock.setsockopt(zmq.SNDTIMEO, 4000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(f"tcp://127.0.0.1:{PORT}")
    sock.send_string("overlay@viz enable 0")
    try:
        print("reply:", sock.recv_string())
    except Exception as exc:  # noqa: BLE001 - the point is to report whatever happens
        print("no reply:", exc)
        proc.kill()
        return 1

    time.sleep(2.5)
    proc.wait(timeout=30)

    frames = sorted(OUT.glob("frame-*.png"))
    if len(frames) < 40:
        print(f"only {len(frames)} frames; ffmpeg said: {proc.stderr.read()[:400]}")
        return 1

    before = centre_pixel(frames[5])
    after = centre_pixel(frames[-3])
    print(f"before the command: rgb{before}")
    print(f"after  the command: rgb{after}")

    if before == after:
        print("FAIL: the frame did not change, so `enable` did not land")
        return 1
    if sum(after) < 40:
        print("PASS: the overlay was bypassed live - standby is achievable")
        return 0
    print("UNCLEAR: the frame changed but is not the background")
    return 1


if __name__ == "__main__":
    sys.exit(main())
