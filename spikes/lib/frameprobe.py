"""Per-frame region colour sampling from a recorded video file.

One FFmpeg pass crops each region, area-averages it down to a single pixel and
hstacks the results, so every output frame is exactly ``len(regions) * 3`` bytes
and stays index-aligned with the source frames.
"""

from __future__ import annotations

import subprocess
from typing import NamedTuple

RGB = tuple[int, int, int]


class Region(NamedTuple):
    name: str
    w: int
    h: int
    x: int
    y: int


def build_filter(regions: list[Region]) -> str:
    n = len(regions)
    if n == 1:
        r = regions[0]
        return (
            f"[0:v]crop={r.w}:{r.h}:{r.x}:{r.y},"
            "scale=1:1:flags=area,setsar=1,format=rgb24[out]"
        )
    parts = ["[0:v]split=%d%s" % (n, "".join(f"[s{i}]" for i in range(n)))]
    for i, r in enumerate(regions):
        parts.append(
            f"[s{i}]crop={r.w}:{r.h}:{r.x}:{r.y},scale=1:1:flags=area,setsar=1[p{i}]"
        )
    parts.append(
        "%shstack=inputs=%d,format=rgb24[out]"
        % ("".join(f"[p{i}]" for i in range(n)), n)
    )
    return ";".join(parts)


def sample(path: str, regions: list[Region]) -> list[list[RGB]]:
    """Returns one entry per frame, each a list of mean RGB per region."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-filter_complex", build_filter(regions),
        "-map", "[out]", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    raw = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, check=True
    ).stdout
    stride = len(regions) * 3
    frames: list[list[RGB]] = []
    for off in range(0, len(raw) - stride + 1, stride):
        frames.append([
            (raw[off + i * 3], raw[off + i * 3 + 1], raw[off + i * 3 + 2])
            for i in range(len(regions))
        ])
    return frames


def deltas(a: list[RGB], b: list[RGB]) -> list[int]:
    """Max per-channel absolute difference for each region."""
    return [max(abs(int(p) - int(q)) for p, q in zip(x, y)) for x, y in zip(a, b)]


def change_points(frames: list[list[RGB]], threshold: int = 3) -> list[tuple[int, list[int]]]:
    """Frame indices where any region moved by more than `threshold`."""
    out = []
    for i in range(1, len(frames)):
        d = deltas(frames[i], frames[i - 1])
        if max(d) > threshold:
            out.append((i, d))
    return out


def fmt(px: RGB) -> str:
    return "#%02X%02X%02X" % px
