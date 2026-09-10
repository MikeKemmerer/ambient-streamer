#!/usr/bin/env python3
"""Does POST /skip actually move the audio on a live channel?

The telnet probe showed `icecast.skip` works; this checks the whole path through
the API, and that the compositor does not restart doing it - the point of the
feature is that audio is a separate process and the video never notices.

Usage: verify-skip.py <channel>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "GET"):
    request = urllib.request.Request(
        BASE + path, data=b"{}" if method == "POST" else None, method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def composer_started(channel: str) -> str:
    names = subprocess.run(
        ["docker", "ps", "--filter", f"name={channel}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    if not names:
        return ""
    return subprocess.run(
        ["docker", "inspect", names[0], "--format", "{{.State.StartedAt}}"],
        capture_output=True, text=True).stdout.strip()


def speed(channel: str) -> str:
    for name in ("progress-next", "progress"):
        path = ROOT / ".run" / channel / name
        if not path.is_file() or path.stat().st_size == 0:
            continue
        text = path.read_bytes()[-3000:].decode("utf-8", "replace")
        for line in reversed(text.splitlines()):
            if line.startswith("speed="):
                return line.split("=", 1)[1].strip()
    return "?"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<42} {note}")
        if not ok:
            failures.append(label)

    _, detail = call(f"/api/channels/{channel}")
    before = detail.get("current_track", "")
    was = composer_started(channel)
    print(f"playing {before.split('/')[-1][:50]}")
    print(f"composer started {was}\n")

    status, body = call(f"/api/channels/{channel}/skip", "POST")
    check("skip accepted", status == 202, str(body.get("detail", body))[:50])
    # Liquidsoap answers Done immediately; the crossfade means the new track
    # takes a few seconds to become the reported current one.
    time.sleep(14)

    _, detail = call(f"/api/channels/{channel}")
    after = detail.get("current_track", "")
    check("the track changed", after != before, after.split("/")[-1][:50])
    check("the compositor did NOT restart", composer_started(channel) == was,
          "audio is a separate process, so the video never noticed")
    check("still holding realtime", speed(channel) >= "0.95", f"speed={speed(channel)}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("skip moves the audio without touching the video")
    return 0


if __name__ == "__main__":
    sys.exit(main())
