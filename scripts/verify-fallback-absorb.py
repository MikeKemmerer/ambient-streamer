#!/usr/bin/env python3
"""Prove an Icecast fallback mount absorbs a Liquidsoap restart.

This is the single most load-bearing claim in the design: Liquidsoap must be
restartable without the compositor noticing, because a compositor restart costs
a new YouTube ingest session. `output.harbor` was measured stalling the
compositor 18.19s on a 9.41s outage; a fallback mount was measured at 0s.

The check is therefore not "did audio come back" but "did the compositor and the
YouTube session survive untouched".
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
CHANNEL = "vibecoding"


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def composer() -> str:
    names = sh("docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}").splitlines()
    return names[0] if names else ""


def pid(container: str) -> str:
    return sh("docker", "inspect", "-f", "{{.State.Pid}}", container)


def path_state() -> tuple[str, int]:
    """readyTime changes only if the RTMP publish restarted - i.e. a new session."""
    raw = sh("docker", "exec", "ambient-mediamtx", "wget", "-qO-", "http://127.0.0.1:9997/v3/paths/list")
    data = json.loads(raw)
    for item in data.get("items", []):
        if item.get("name") == CHANNEL:
            return item.get("readyTime", ""), item.get("bytesSent", 0)
    return "", 0


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<46} {note}")
        if not ok:
            failures.append(label)

    box = composer()
    print(f"a Liquidsoap restart must not reach the compositor ({box})")

    before_pid = pid(box)
    before_ready, before_bytes = path_state()
    print(f"  baseline: composer pid {before_pid}, session since {before_ready}")

    t0 = time.time()
    sh("docker", "restart", f"{CHANNEL}-liquidsoap")
    print(f"  liquidsoap restarted in {time.time() - t0:.1f}s, watching 60s...")
    time.sleep(60)

    after_pid = pid(composer())
    after_ready, after_bytes = path_state()

    check("compositor never restarted", after_pid == before_pid and before_pid != "", f"pid {after_pid}")
    check("YouTube session unbroken", after_ready == before_ready and before_ready != "", after_ready or "path gone")
    check("relay still sending", after_bytes > before_bytes, f"+{after_bytes - before_bytes} bytes")

    kicked = sh("docker", "logs", "ambient-icecast", "--since", "90s")
    check(
        "compositor not dropped by Icecast",
        "fallen too far behind" not in kicked,
        "no 'fallen too far behind'" if "fallen too far behind" not in kicked else "was dropped",
    )

    progress = (ROOT / ".run" / CHANNEL / "progress").read_bytes().decode("utf-8", "replace")
    speed = ""
    drops = ""
    for line in progress.splitlines()[-60:]:
        if line.startswith("speed="):
            speed = line.split("=", 1)[1]
        elif line.startswith("drop_frames="):
            drops = line.split("=", 1)[1]
    check("no dropped frames", drops == "0", f"drop_frames={drops}")
    print(f"        speed={speed} (cumulative average since compositor start)")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the fallback absorbed it - the compositor never noticed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
