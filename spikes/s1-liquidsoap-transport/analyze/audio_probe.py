#!/usr/bin/env python3
"""Analyze a raw s16le recording: silence runs and per-channel dominant tone.

Test media carries a different sine in each channel, so a post-reconnect channel
swap or byte-misaligned PCM resync shows up as a changed or absent tone pair.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np


def load(path: str, channels: int) -> np.ndarray:
    raw = np.fromfile(path, dtype="<i2")
    usable = (raw.size // channels) * channels
    return raw[:usable].reshape(-1, channels).astype(np.float32) / 32768.0


def analyze(
    data: np.ndarray, rate: int, window: float, silence_db: float
) -> dict[str, Any]:
    n = max(1, int(rate * window))
    frames = data.shape[0] // n
    if frames == 0:
        return {"duration_s": 0.0, "windows": 0, "silence_runs": [], "tone_segments": []}

    trimmed = data[: frames * n].reshape(frames, n, data.shape[1])
    rms = np.sqrt(np.mean(trimmed**2, axis=1))
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-9))

    win = np.hanning(n).astype(np.float32)
    spec = np.abs(np.fft.rfft(trimmed * win[None, :, None], axis=1))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    dominant = freqs[np.argmax(spec, axis=1)]

    loud = rms_db.max(axis=1) >= silence_db

    silence_runs = []
    start = None
    for i, is_loud in enumerate(loud):
        if not is_loud and start is None:
            start = i
        elif is_loud and start is not None:
            silence_runs.append(
                {"start_s": round(start * window, 3), "duration_s": round((i - start) * window, 3)}
            )
            start = None
    if start is not None:
        silence_runs.append(
            {"start_s": round(start * window, 3), "duration_s": round((frames - start) * window, 3)}
        )

    loud_runs = 0
    prev = False
    for is_loud in loud:
        if is_loud and not prev:
            loud_runs += 1
        prev = bool(is_loud)

    # Collapse windows into segments of a stable tone pair (5 Hz quantization).
    segments = []
    key_prev = None
    seg_start = 0
    for i in range(frames):
        key = None if not loud[i] else tuple(int(round(f / 5.0)) for f in dominant[i])
        if key != key_prev:
            if key_prev is not None:
                segments.append((seg_start, i, key_prev))
            seg_start = i
            key_prev = key
    if key_prev is not None:
        segments.append((seg_start, frames, key_prev))

    tone_segments = [
        {
            "start_s": round(a * window, 3),
            "duration_s": round((b - a) * window, 3),
            "hz": [k * 5 for k in key],
        }
        for a, b, key in segments
        if (b - a) * window >= 0.5
    ]

    return {
        "duration_s": round(data.shape[0] / rate, 3),
        "windows": frames,
        "silence_pct": round(100.0 * float((~loud).sum()) / frames, 2),
        "loud_runs": loud_runs,
        "max_silence_run_s": round(max((r["duration_s"] for r in silence_runs), default=0.0), 3),
        "silence_runs": [r for r in silence_runs if r["duration_s"] >= window * 2],
        "tone_segments": tone_segments,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--window", type=float, default=0.1)
    ap.add_argument("--silence-db", type=float, default=-60.0)
    args = ap.parse_args()

    data = load(args.path, args.channels)
    print(json.dumps(analyze(data, args.rate, args.window, args.silence_db), indent=2))


if __name__ == "__main__":
    main()
