#!/usr/bin/env python3
"""Average a channel's composer CPU over time.

A single `docker stats` sample lands wherever the slideshow happens to be. A
crossfade decodes and blends two images at once, so instantaneous readings swing
by tens of percent and are useless for comparing encoders.
"""
from __future__ import annotations

import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"


def composer(name: str) -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    return out[0] if out else ""


def sample(boxes: list[str]) -> dict[str, float]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", *boxes],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    values: dict[str, float] = {}
    for line in out:
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = float(parts[1].rstrip("%"))
            except ValueError:
                pass
    return values


def gpu() -> tuple[int, int]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=utilization.encoder,memory.used",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    ).stdout.strip()
    enc, mem = (int(x.strip()) for x in out.split(","))
    return enc, mem


def speed(name: str) -> str:
    text = (ROOT / ".run" / name / "progress").read_bytes()[-3000:].decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("speed="):
            return line.split("=", 1)[1].strip()
    return "?"


def main() -> int:
    names = sys.argv[1:] or ["vibecoding", "westcoastclassics"]
    rounds = 12
    interval = 5

    boxes = {n: composer(n) for n in names}
    boxes = {n: b for n, b in boxes.items() if b}
    if not boxes:
        print("no running composers")
        return 1

    series: dict[str, list[float]] = {n: [] for n in boxes}
    enc_series: list[int] = []

    print(f"sampling {rounds} times, {interval}s apart ({rounds * interval}s total)")
    for _ in range(rounds):
        values = sample(list(boxes.values()))
        for name, box in boxes.items():
            if box in values:
                series[name].append(values[box])
        enc_series.append(gpu()[0])
        time.sleep(interval)

    print()
    for name, points in series.items():
        if not points:
            continue
        print(f"  {name}")
        print(f"    cpu   mean {statistics.mean(points):6.1f}%   "
              f"median {statistics.median(points):6.1f}%   "
              f"min {min(points):5.1f}%   max {max(points):5.1f}%")
        print(f"    speed {speed(name)}")
    enc, mem = gpu()
    print(f"\n  gpu encoder mean {statistics.mean(enc_series):.1f}%  now {enc}%  {mem} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
