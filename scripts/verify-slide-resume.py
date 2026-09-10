#!/usr/bin/env python3
"""Restart the real producer and see whether it resumes on the same slide.

Runs slideshow.py itself in a throwaway container rather than restarting a live
channel: the thing under test is the producer's own state, and a broadcast does
not need interrupting to observe it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/tmp/slide-resume")
SLIDES = WORK / "slides"
LIST = WORK / "images.list"
POSITION = WORK / "slide-position"
PRODUCER = "/opt/ambient/slideshow.py"
COLOURS = ["red", "green", "blue", "yellow", "magenta"]
HOLD = 2.0


def make_slides() -> list[str]:
    SLIDES.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, colour in enumerate(COLOURS):
        path = SLIDES / f"{index:02d}-{colour}.png"
        if not path.is_file():
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", f"color=c={colour}:s=320x240",
                 "-frames:v", "1", str(path)],
                check=True)
        paths.append(str(path))
    LIST.write_text("\n".join(paths) + "\n", encoding="utf-8")
    return paths


def run_producer(seconds: float) -> str:
    """Run the producer for a while, discarding its frames."""
    env = {
        **os.environ,
        "IMAGES_LIST": str(LIST),
        "HOLD_SECONDS": str(HOLD),
        "FADE_SECONDS": "0.2",
        "PRODUCER_FPS": "10",
        "WIDTH": "320",
        "HEIGHT": "240",
        "SLIDE_POSITION_FILE": str(POSITION),
        "COLOR_MODE": "manual",
    }
    proc = subprocess.Popen(
        [sys.executable, PRODUCER],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
    )
    time.sleep(seconds)
    proc.send_signal(signal.SIGTERM)
    try:
        _, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err or ""


def saved() -> str:
    return POSITION.read_text(encoding="utf-8").strip() if POSITION.is_file() else ""


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    paths = make_slides()
    POSITION.unlink(missing_ok=True)

    print(f"first run: {len(paths)} slides at {HOLD}s each")
    err = run_producer(HOLD * 3.5)
    check("the producer started", "slide" in err or saved() != "",
          err.strip().splitlines()[-1][:70] if err.strip() else "")
    check("a position was written", bool(saved()))
    if not saved():
        return 1

    left_off = saved()
    print(f"  left off on {os.path.basename(left_off)} (index {paths.index(left_off)})")
    check("moved past the first slide", left_off != paths[0], os.path.basename(left_off))

    print("\nsecond run: producer restarted, list unchanged")
    err = run_producer(HOLD * 0.5)
    check("announced the resume", "slide_resume" in err,
          "logged where it was picking up")
    check("resumed on the slide it left off on", saved() == left_off,
          f"{os.path.basename(left_off)} -> {os.path.basename(saved())}")

    print("\nthird run: the saved slide is no longer in the list")
    LIST.write_text("\n".join(p for p in paths if p != left_off) + "\n", encoding="utf-8")
    POSITION.write_text(left_off, encoding="utf-8")
    run_producer(HOLD * 0.5)
    check("fell back instead of stalling", saved() != left_off and saved() in paths,
          os.path.basename(saved()) if saved() else "nothing")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the producer resumes on the slide it left off on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
