#!/usr/bin/env python3
"""Where do the seconds between RTMP connect and first packet go?

The replacement composer opens its RTMP output before it has anything to send.
The relay sets overridePublisher, so that connect evicts the outgoing composer
immediately and everything ffmpeg does afterwards is dead air. This measures
that "afterwards" for the real command shape and for candidate fixes.

Runs against the live Icecast mount but writes to `-f null`, so nothing is
published and no channel is disturbed.

Usage: bench-first-packet.py <icecast-url> [repeats]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WIDTH, HEIGHT, FPS = 1280, 720, 30
PRODUCER_FPS = 10
OUT_TIME = re.compile(r"^out_time_us=(\d+)", re.M)

# The graph the channel actually runs, trimmed to one output: the question is
# when the first packet appears, not what it looks like.
GRAPH = (
    f"[1:v]fps={FPS}:start_time=0,realtime,format=yuv420p,setsar=1[base];"
    f"[0:a]showfreqs@viz=s={WIDTH}x{HEIGHT}:mode=bar:ascale=log:fscale=log:"
    f"win_size=1024:averaging=2:colors=0x4FC3F7|0x4FC3F7,fps={FPS},"
    "format=yuv420p,setsar=1[viz0];"
    "[viz0]hue@viz=h=0:s=1,split=2[vizc][vizm];"
    "[vizm]format=gray,lut@vizop=y='val*0.65'[vizalpha];"
    "[vizc][vizalpha]alphamerge[vizrgba];"
    "[base][vizrgba]overlay@viz=eof_action=pass:format=auto:enable=1,"
    "eq@eq=eval=frame:contrast=1,format=yuv420p,setsar=1[vmain]"
)

VARIANTS: dict[str, list[str]] = {
    "today": ["-probesize", "32k", "-analyzeduration", "500000"],
    "bigger queues": ["-probesize", "32k", "-analyzeduration", "500000",
                      "-thread_queue_size", "512"],
    "nobuffer": ["-probesize", "32k", "-analyzeduration", "500000",
                 "-fflags", "nobuffer", "-thread_queue_size", "512"],
    "tiny probe": ["-probesize", "4k", "-analyzeduration", "0",
                   "-fflags", "nobuffer", "-thread_queue_size", "512"],
}


def slides_source() -> list[str]:
    """A still image on repeat stands in for the producer's pipe."""
    return ["-f", "lavfi", "-i", f"color=c=navy:s={WIDTH}x{HEIGHT}:r={PRODUCER_FPS}"]


def run_once(url: str, input_flags: list[str]) -> float | None:
    with tempfile.TemporaryDirectory() as directory:
        graph_file = Path(directory) / "graph.txt"
        graph_file.write_text(GRAPH, encoding="utf-8")
        progress = Path(directory) / "progress"

        argv = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-progress", str(progress),
            *input_flags,
            "-i", url,
            *slides_source(),
            "-filter_complex_script", str(graph_file),
            "-map", "[vmain]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3000k",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-f", "null", "-",
        ]
        started = time.monotonic()
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)
        first: float | None = None
        try:
            while time.monotonic() - started < 40:
                if proc.poll() is not None:
                    break
                try:
                    text = progress.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                match = OUT_TIME.search(text)
                if match and int(match.group(1)) > 0:
                    first = time.monotonic() - started
                    break
                time.sleep(0.05)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return first


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    print(f"time from process start to first encoded packet, {repeats} runs each")
    print(f"source: {url}\n")
    results: dict[str, list[float]] = {}
    for label, flags in VARIANTS.items():
        times = []
        for _ in range(repeats):
            value = run_once(url, flags)
            if value is not None:
                times.append(value)
            time.sleep(1)
        results[label] = times
        shown = ", ".join(f"{t:.2f}s" for t in times) or "no packet in 40s"
        best = f"  best {min(times):.2f}s" if times else ""
        print(f"  {label:<14} {shown}{best}")

    print()
    ranked = sorted(((min(v), k) for k, v in results.items() if v))
    if not ranked:
        print("nothing produced a packet; the source may be unreachable")
        return 1
    for value, label in ranked:
        print(f"  {label:<14} {value:.2f}s")
    baseline = min(results.get("today") or [0]) or None
    if baseline and ranked[0][0] < baseline:
        print(f"\n  best saves {baseline - ranked[0][0]:.2f}s against today")
    else:
        print("\n  nothing beat today's flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
