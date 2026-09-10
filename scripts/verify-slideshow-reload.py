#!/usr/bin/env python3
"""Prove the slideshow producer picks up list changes without stalling the pipe.

The pipe is the thing under test: FFmpeg reads it, and a stall or EOF ends a
24/7 broadcast. So every check here is "did bytes keep flowing", not "did the
function return".
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    work = tempfile.mkdtemp(prefix="slideshow-reload-")
    made = {}
    for name, color in (("a.png", (200, 30, 30)), ("b.png", (30, 200, 30)), ("c.png", (30, 30, 200))):
        path = os.path.join(work, name)
        Image.new("RGB", (1280, 720), color).save(path)
        made[name] = path

    listing = os.path.join(work, "images.list")

    def write_list(paths: list[str]) -> None:
        # exactly how the backend writes it: temp in the same dir, atomic rename
        tmp = listing + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("".join(p + "\n" for p in paths))
        os.replace(tmp, listing)

    write_list([made["a.png"], made["b.png"]])

    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "ffmpeg", "slideshow.py"),
            "--images-list", listing,
            "--width", "1280",
            "--height", "720",
            "--fps", "10",
            "--hold", "2",
            "--fade", "1",
        ],
        stdout=subprocess.PIPE,
        stderr=open(os.path.join(work, "stderr.log"), "wb"),
    )

    total = [0]
    running = [True]

    def drain() -> None:
        while running[0]:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            total[0] += len(chunk)

    threading.Thread(target=drain, daemon=True).start()

    def settle(seconds: float) -> int:
        time.sleep(seconds)
        return total[0]

    failures = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<38} {note}")
        if not ok:
            failures.append(label)

    print("slideshow live-reload verification")
    baseline = settle(4.0)
    check("streams at all", baseline > 0, f"{baseline} bytes")

    write_list([made["a.png"], made["b.png"], made["c.png"]])
    changed_at = time.time()
    after_add = settle(6.0)
    check(
        "survives atomic list replace",
        after_add > baseline,
        f"+{after_add - baseline} bytes in {time.time() - changed_at:.1f}s",
    )

    os.remove(made["b.png"])
    after_delete = settle(5.0)
    check("survives a deleted image", after_delete > after_add, f"+{after_delete - after_add} bytes")

    write_list([])
    after_empty = settle(4.0)
    check("survives an empty list", after_empty > after_delete, f"+{after_empty - after_delete} bytes")

    write_list([made["a.png"], made["c.png"]])
    after_restore = settle(4.0)
    check("recovers when list returns", after_restore > after_empty, f"+{after_restore - after_empty} bytes")
    check("producer still alive", proc.poll() is None, "no exit")

    running[0] = False
    proc.kill()
    proc.wait(timeout=10)

    if failures:
        print()
        print("producer stderr:")
        with open(os.path.join(work, "stderr.log"), encoding="utf-8", errors="replace") as handle:
            for line in handle.read().splitlines()[-15:]:
                print(f"  {line}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed - the pipe never stalled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
