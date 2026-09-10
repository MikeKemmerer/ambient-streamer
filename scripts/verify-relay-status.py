#!/usr/bin/env python3
"""The relay status a channel reports must not depend on it being selected.

The UI reads the list for unselected channels and the detail endpoint for the
one in focus. If those two disagree, clicking away from a channel makes it look
like the YouTube leg dropped.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def get(path: str) -> dict:
    request = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    listed = {c["name"]: c for c in get("/api/channels")["channels"]}
    failures = []
    print("relay status must be the same selected or not")
    for name, row in listed.items():
        detail = get(f"/api/channels/{name}")
        same = row["rtmp"] == detail["rtmp"] and row["hls"] == detail["hls"]
        print(f"  {'PASS' if same else 'FAIL'}  {name:<16} "
              f"list rtmp={row['rtmp']}/hls={row['hls']}   "
              f"detail rtmp={detail['rtmp']}/hls={detail['hls']}")
        if not same:
            failures.append(name)

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the list and the detail agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
