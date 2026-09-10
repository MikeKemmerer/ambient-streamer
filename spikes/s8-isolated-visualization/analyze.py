#!/usr/bin/env python3
"""Validate fallback and plugin windows in the S8 lossless output."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def frame(path: str, at: float, width: int, height: int) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(at),
            "-i", path, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    )
    expected = width * height * 3
    if len(result.stdout) != expected:
        raise RuntimeError(f"frame at {at}s is {len(result.stdout)} bytes, expected {expected}")
    return result.stdout


def pixel(data: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return tuple(data[offset:offset + 3])  # type: ignore[return-value]


def distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return sum(abs(a - b) for a, b in zip(left, right))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("status")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    args = parser.parse_args()

    samples = {
        "initial_null": 1.0,
        "plugin_a": 3.5,
        "restart_gap": 6.2,
        "plugin_b": 9.5,
        "final_null": 13.5,
    }
    observed = {}
    for name, at in samples.items():
        data = frame(args.output, at, args.width, args.height)
        observed[name] = {
            "a": pixel(data, args.width, args.width // 4, args.height // 3),
            "b": pixel(data, args.width, args.width * 3 // 4, args.height // 3),
            "base": pixel(data, args.width, args.width // 2, args.height * 4 // 5),
        }

    for name in ("initial_null", "restart_gap", "final_null"):
        value = observed[name]
        assert distance(value["a"], value["base"]) < 12, (name, value)
        assert distance(value["b"], value["base"]) < 12, (name, value)
    assert distance(observed["plugin_a"]["a"], observed["plugin_a"]["base"]) > 80
    assert distance(observed["plugin_a"]["b"], observed["plugin_a"]["base"]) < 12
    assert distance(observed["plugin_b"]["b"], observed["plugin_b"]["base"]) > 80
    assert distance(observed["plugin_b"]["a"], observed["plugin_b"]["base"]) < 12

    status = json.loads(Path(args.status).read_text(encoding="utf-8"))
    assert status["reconnects"] >= 2, status
    assert status["fallbacks"] > 0, status
    print(json.dumps({"samples": observed, "framekeeper": status}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())