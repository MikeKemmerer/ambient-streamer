#!/usr/bin/env python3
"""How long is YouTube actually without data during a settings restart?

The relay is the honest witness. `runOnReady` runs the YouTube publisher only
while the path has a publisher, so the window between `runOnReady command
stopped` and `runOnReady command started` is exactly the dead air - and each
restart of it is a new YouTube ingest session.

Usage: measure-restart-gap.py <channel>   (this RESTARTS the channel)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)")


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "POST") -> dict:
    request = urllib.request.Request(
        BASE + path, data=b"{}", method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read() or b"{}")


def relay_events(channel: str, since: str) -> list[tuple[datetime, str]]:
    out = subprocess.run(
        ["docker", "logs", "-t", "--since", since, "ambient-mediamtx"],
        capture_output=True, text=True).stdout + subprocess.run(
        ["docker", "logs", "-t", "--since", since, "ambient-mediamtx"],
        capture_output=True, text=True).stderr
    events = []
    for line in out.splitlines():
        if f"[path {channel}]" not in line or "runOnReady" not in line:
            continue
        match = STAMP.match(line)
        if not match:
            continue
        when = datetime.strptime(match.group(1)[:26], "%Y-%m-%dT%H:%M:%S.%f")
        events.append((when, "stopped" if "stopped" in line else "started"))
    return events


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    since = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"restarting {channel} and watching the relay")
    call(f"/api/channels/{channel}/restart")
    time.sleep(90)

    events = relay_events(channel, since)
    if not events:
        print("no runOnReady transitions seen; the publisher may not be configured")
        return 1

    print("\n  relay transitions:")
    for when, kind in events:
        print(f"    {when.strftime('%H:%M:%S.%f')[:-3]}  runOnReady {kind}")

    gaps = []
    pending = None
    for when, kind in events:
        if kind == "stopped":
            pending = when
        elif pending is not None:
            gaps.append((when - pending).total_seconds())
            pending = None

    print()
    if not gaps:
        print("the YouTube publisher never stopped: no ingest session was lost")
        return 0
    for gap in gaps:
        print(f"  dead air: {gap:.2f}s (a new YouTube ingest session)")
    print(f"\n  worst: {max(gaps):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
