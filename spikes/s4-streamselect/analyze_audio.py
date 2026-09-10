#!/usr/bin/env python3
"""Estimate the dominant frequency per window of a WAV file via zero crossings.

Used to prove which branch astreamselect routed to the output without needing
numpy on the test host.
"""

from __future__ import annotations

import argparse
import json
import struct
import wave


def windows(path: str, window_s: float):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        if width != 2:
            raise SystemExit(f"expected 16-bit PCM, got {width * 8}-bit")
        raw = w.readframes(w.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    mono = samples[0::channels]
    size = int(rate * window_s)
    for start in range(0, len(mono) - size, size):
        seg = mono[start:start + size]
        crossings = sum(1 for a, b in zip(seg, seg[1:]) if (a >= 0) != (b >= 0))
        peak = max(abs(s) for s in seg)
        yield start / rate, crossings * (rate / size) / 2.0, peak


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("commands")
    ap.add_argument("--window", type=float, default=0.100)
    args = ap.parse_args()

    with open(args.commands, encoding="utf-8") as fh:
        rows = json.load(fh)

    data = list(windows(args.wav, args.window))
    print(f"windows analysed : {len(data)} of {args.window * 1000:.0f} ms")
    print("\n  t(s)   est. freq (Hz)  peak")
    prev = None
    changes = []
    for t, freq, peak in data:
        tag = ""
        if prev is not None and abs(freq - prev) > 100:
            tag = "  <== frequency change"
            changes.append((t, prev, freq))
        prev = freq
        print(f"  {t:5.2f}  {freq:14.1f}  {peak:5d}{tag}")

    print("\ncommands sent")
    for row in rows:
        print(f"  t+{row['t_rel']:6.3f}s  reply={row['reply']!r}  {row['msg']}")

    print("\ndetected frequency changes")
    for t, a, b in changes:
        print(f"  t={t:5.2f}s  {a:.0f} Hz -> {b:.0f} Hz")
    if not changes:
        print("  NONE -- astreamselect did not switch")


if __name__ == "__main__":
    main()
