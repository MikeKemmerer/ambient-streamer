#!/usr/bin/env python3
"""Generate the original starter soundboard effects as 44.1 kHz stereo WAV."""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path
from typing import Callable

RATE = 44_100
PEAK = 0.68
Sample = tuple[float, float]


def envelope(t: float, duration: float, attack: float, release: float) -> float:
    return min(1.0, t / attack, (duration - t) / release)


def write_effect(path: Path, duration: float, synth: Callable[[float, int], Sample]) -> None:
    frames = [synth(index / RATE, index) for index in range(round(duration * RATE))]
    highest = max(abs(value) for frame in frames for value in frame) or 1.0
    scale = PEAK / highest
    payload = b"".join(
        struct.pack(
            "<hh",
            round(max(-1.0, min(1.0, left * scale)) * 32767),
            round(max(-1.0, min(1.0, right * scale)) * 32767),
        )
        for left, right in frames
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(payload)


def air_horn(t: float, _index: int) -> Sample:
    duration = 1.35
    bend = 1.0 + 0.018 * math.exp(-4.0 * t)
    tone = 0.0
    for base in (466.16, 587.33):
        for harmonic in range(1, 6):
            tone += math.sin(2.0 * math.pi * base * bend * harmonic * t) / harmonic
    tremolo = 0.94 + 0.06 * math.sin(2.0 * math.pi * 7.0 * t)
    value = tone * tremolo * envelope(t, duration, 0.035, 0.24)
    return value, value


def metal_pipe(t: float, index: int) -> Sample:
    modes = ((311.0, 2.4), (487.0, 2.0), (733.0, 1.7), (1091.0, 1.4), (1613.0, 1.1))
    ringing = sum(
        math.sin(2.0 * math.pi * frequency * t + mode) * math.exp(-decay * t)
        for mode, (frequency, decay) in enumerate(modes)
    )
    rng = random.Random(9000 + index)
    impact = (rng.random() * 2.0 - 1.0) * math.exp(-38.0 * t)
    second = 0.0
    if t > 0.38:
        shifted = t - 0.38
        second = 0.45 * math.sin(2.0 * math.pi * 641.0 * shifted) * math.exp(-4.2 * shifted)
    value = (ringing * 0.42 + impact * 2.5 + second) * envelope(t, 2.4, 0.004, 0.25)
    return value * 0.96, value


CLAP_RNG = random.Random(20260910)
CLAPS = [
    (CLAP_RNG.uniform(0.0, 2.75), CLAP_RNG.uniform(0.45, 1.0), CLAP_RNG.uniform(-0.75, 0.75))
    for _ in range(72)
]


def applause(t: float, index: int) -> Sample:
    rng = random.Random(17000 + index)
    noise = rng.random() * 2.0 - 1.0
    left = right = 0.0
    for start, strength, pan in CLAPS:
        age = t - start
        if 0.0 <= age < 0.075:
            burst = noise * strength * math.exp(-42.0 * age) * (0.7 + 0.3 * math.sin(2.0 * math.pi * 1800.0 * age))
            left += burst * (1.0 - max(0.0, pan))
            right += burst * (1.0 + min(0.0, pan))
    room = noise * 0.055 * envelope(t, 3.0, 0.2, 0.35)
    return left + room, right + room


def record_scratch(t: float, index: int) -> Sample:
    duration = 1.05
    rng = random.Random(26000 + index)
    noise = rng.random() * 2.0 - 1.0
    sweep = 2100.0 * (1.0 - t / duration) + 190.0
    wobble = math.sin(2.0 * math.pi * (sweep * t + 5.0 * math.sin(18.0 * t)))
    chops = 0.35 + 0.65 * max(0.0, math.sin(2.0 * math.pi * 9.0 * t))
    value = (0.72 * noise + 0.28 * wobble) * chops * envelope(t, duration, 0.01, 0.12)
    return value, -value * 0.88


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "common" / "soundboard"
    effects = (
        ("air-horn.wav", 1.35, air_horn),
        ("metal-pipe.wav", 2.4, metal_pipe),
        ("applause.wav", 3.0, applause),
        ("record-scratch.wav", 1.05, record_scratch),
    )
    for name, duration, synth in effects:
        path = target / name
        write_effect(path, duration, synth)
        print(path.relative_to(target.parents[1]))


if __name__ == "__main__":
    main()