#!/usr/bin/env python3
"""Prove a manual color sticks instead of being overwritten by the producer.

The reported bug was that the Look tab did nothing and the color stayed put, so
the check that matters is whether the producer stops re-colouring from the image
once the operator takes manual control - and whether that happens without
restarting anything.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8090"
ROOT = Path.home() / "ambient-streamer"
CHANNEL = "vibecoding"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def composer() -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    return out[0] if out else ""


def producer_color_events(container: str, since: str) -> int:
    """Count colour commands the producer logged since a timestamp."""
    out = subprocess.run(
        ["docker", "logs", container, "--since", since],
        capture_output=True,
        text=True,
    )
    text = out.stdout + out.stderr
    return len(re.findall(r"event=color_sent|hue@hue", text))


def pid(container: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", container], capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    box = composer()
    print(f"manual color holds on the live channel ({box})")
    before_pid = pid(box)

    status, body = call(
        f"/api/channels/{CHANNEL}/color",
        "PUT",
        {"mode": "manual", "manual": {"accent": "#FF8A3D", "tint": "#2A1206"}},
    )
    check("color accepted", status == 200, f"HTTP {status}")
    check("commands were sent", bool(body.get("commands")), f"{len(body.get('commands') or [])} ZMQ commands")

    mode_file = ROOT / ".run" / CHANNEL / "color-mode"
    time.sleep(2)
    check(
        "mode published for the producer",
        mode_file.exists() and mode_file.read_text().strip() == "manual",
        mode_file.read_text().strip() if mode_file.exists() else "missing",
    )

    # The bug: the producer re-colours on every slide change and wins.
    print("\n  watching 45s for the producer to overwrite it...")
    mark = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    time.sleep(45)
    overwrites = producer_color_events(box, mark)
    check("producer sent no color commands", overwrites == 0, f"{overwrites} in 45s")

    status, after = call(f"/api/channels/{CHANNEL}?detail=true")
    mode = ((after.get("config") or {}).get("color") or {}).get("mode")
    check("config still manual", mode == "manual", str(mode))
    check("composer never restarted", pid(box) == before_pid and before_pid != "", f"pid {before_pid}")

    speed = ""
    progress = ROOT / ".run" / CHANNEL / "progress"
    for line in progress.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
        if line.startswith("speed="):
            speed = line.split("=", 1)[1]
    check("still realtime", speed.rstrip("x").replace(" ", "") >= "0.95", f"speed={speed}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the operator's color survives - the producer stood down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
