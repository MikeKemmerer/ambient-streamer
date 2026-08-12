#!/usr/bin/env python3
"""Prove the composite grade actually changes the picture.

The previous graph applied eq/hue to the background only, then screen-blended the
visualization over it - and screening the chroma planes pinned U and V near 192,
a magenta cast nothing upstream could survive. Both faults are invisible in a
"command accepted" check, because FFmpeg replied `0 Success` to commands that
changed nothing. So this measures pixels, not replies.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ZMQ_PORT = 5799


def build_graph(accent: str = "0x4FC3F7") -> str:
    viz = (
        f"[0:a]showfreqs@viz0=s=640x360:mode=bar:ascale=log:fscale=log:"
        f"win_size=1024:averaging=2:colors={accent}|{accent},fps=30,"
        f"format=yuv420p,setsar=1[viz0];"
        f"[0:a]showwaves@viz1=s=640x360:mode=line:rate=30:draw=scale:scale=sqrt:"
        f"split_channels=0:colors={accent}|{accent},fps=30,"
        f"format=yuv420p,setsar=1[viz1];"
    )
    return (
        f"[1:v]fps=30:start_time=0,"
        f"zmq@ctl=bind_address=tcp\\://127.0.0.1\\:{ZMQ_PORT},"
        f"format=yuv420p,setsar=1[base];"
        f"{viz}"
        f"[viz0][viz1]streamselect@sel=inputs=2:map=0,"
        f"hue@viz=h=0:s=1[viz];"
        f"[base][viz]blend=c0_mode=screen:c1_mode=grainmerge:c2_mode=grainmerge"
        f":all_opacity=0.65,"
        f"eq@eq=eval=frame:contrast=1:brightness=0:saturation=1"
        f":gamma_r=1:gamma_g=1:gamma_b=1,"
        f"hue@hue=h=0,format=yuv420p,setsar=1[vfull]"
    )


def mean_rgb(png: Path) -> tuple[int, int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "lavfi",
         f"movie={png},signalstats", "-show_entries",
         "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.UAVG,lavfi.signalstats.VAVG",
         "-of", "csv=p=0", "-read_intervals", "%+#1"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not out:
        return (-1, -1, -1)
    y, u, v = (float(x) for x in out[0].split(","))
    r = y + 1.402 * (v - 128)
    g = y - 0.344136 * (u - 128) - 0.714136 * (v - 128)
    b = y + 1.772 * (u - 128)
    return tuple(max(0, min(255, round(c))) for c in (r, g, b))  # type: ignore


def zmq_send(target: str, command: str, value: str) -> str:
    code = (
        "import zmq,sys\n"
        "c=zmq.Context();s=c.socket(zmq.REQ)\n"
        "s.setsockopt(zmq.RCVTIMEO,4000);s.setsockopt(zmq.SNDTIMEO,4000)\n"
        f"s.connect('tcp://127.0.0.1:{ZMQ_PORT}')\n"
        "s.send_string(' '.join(sys.argv[1:]))\n"
        "print(s.recv().decode(errors='replace'))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, target, command, value],
        capture_output=True, text=True,
    ).stdout.strip()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="colorgrade-"))
    # A deliberately magenta-ish source, like the channel's real artwork.
    still = work / "still.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=0xE8A0C0:s=640x360", "-frames:v", "1", str(still)],
        check=True,
    )

    graph = work / "graph.txt"
    graph.write_text(build_graph(), encoding="utf-8")

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
         "-loop", "1", "-framerate", "30", "-i", str(still),
         "-filter_complex", graph.read_text(encoding="utf-8"),
         "-map", "[vfull]", "-f", "image2", "-update", "1",
         "-y", str(work / "out.png")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(6)

    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<40} {note}")
        if not ok:
            failures.append(label)

    print("the operator's grade must change the finished picture")
    if proc.poll() is not None:
        print("  ffmpeg exited early:")
        print("   ", proc.stderr.read().decode(errors="replace")[-600:])
        return 1

    base = mean_rgb(work / "out.png")
    check("graph runs", base != (-1, -1, -1), f"rgb{base}")

    reply = zmq_send("eq@eq", "saturation", "0")
    time.sleep(3)
    grey = mean_rgb(work / "out.png")
    spread_before = max(base) - min(base)
    spread_after = max(grey) - min(grey)
    check("saturation 0 desaturates", spread_after < spread_before / 2,
          f"rgb{base} spread {spread_before} -> rgb{grey} spread {spread_after}  [{reply}]")

    zmq_send("eq@eq", "saturation", "1")
    time.sleep(2)
    reply = zmq_send("hue@hue", "h", "120")
    time.sleep(3)
    turned = mean_rgb(work / "out.png")
    moved = sum(abs(a - b) for a, b in zip(base, turned))
    check("hue 120 rotates the composite", moved > 40,
          f"rgb{base} -> rgb{turned} (delta {moved})  [{reply}]")

    reply = zmq_send("eq@eq", "brightness", "-1")
    time.sleep(3)
    dark = mean_rgb(work / "out.png")
    check("brightness -1 darkens", sum(dark) < sum(base), f"rgb{dark}  [{reply}]")

    proc.kill()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the grade reaches the composite - not just the background")
    return 0


if __name__ == "__main__":
    sys.exit(main())
