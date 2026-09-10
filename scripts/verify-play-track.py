#!/usr/bin/env python3
"""Play a chosen track through the API, on a live channel.

Proves the whole path: HTTP -> request queue -> Icecast mount -> what the
status reports. Also proves the endpoint refuses a track this channel does not
have; `queue.push` would otherwise resolve any path or URL on the Liquidsoap
container.

Usage: verify-play-track.py <channel>
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8090"
TOKEN = ""


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    global TOKEN
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    for line in (Path.home() / "ambient-streamer" / ".env").read_text().splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip().strip('"')

    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<46} {note}")
        if not ok:
            failures.append(label)

    code, listing = call("GET", f"/api/channels/{channel}/playlist")
    if code != 200:
        print(f"cannot read the playlist: {code} {listing}")
        return 2
    paths = listing["container_paths"]
    shown = listing["resolved"]

    _, status = call("GET", f"/api/channels/{channel}")
    playing = status.get("current_track") or ""
    print(f"playing   {playing.split('/')[-1][:52]}")

    pick = next((i for i in reversed(range(len(paths))) if shown[i] != playing), 0)
    print(f"asking    {shown[pick].split('/')[-1][:52]}\n")

    code, body = call("POST", f"/api/channels/{channel}/play", {"track": paths[pick]})
    check("accepted", code == 202, f"{code} {body.get('detail', body.get('error', ''))}")

    time.sleep(14)
    _, after = call("GET", f"/api/channels/{channel}")
    now = after.get("current_track") or ""
    check("the chosen track is on air", now == shown[pick], now.split("/")[-1][:52])
    check("it interrupted the current track", now != playing)

    code, body = call("POST", f"/api/channels/{channel}/play", {"track": "/etc/passwd"})
    check("a path off the channel is refused", code == 404, f"{code} {body.get('error')}")

    code, body = call(
        "POST", f"/api/channels/{channel}/play",
        {"track": "http://example.invalid/x.mp3"},
    )
    check("a URL is refused", code == 404, f"{code} {body.get('error')}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("play works end to end and only accepts this channel's own tracks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
