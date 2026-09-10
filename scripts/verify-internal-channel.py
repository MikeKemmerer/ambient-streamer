#!/usr/bin/env python3
"""An internal channel: does it serve HLS, and is the YouTube leg really absent?

The claim under test is structural, not "the stream key is blank": the composer
publishes only the local rendition, so the relay's program path never goes ready
and `runOnReady` — the only thing that ever talks to YouTube — has nothing to
fire on.

Usage: verify-internal-channel.py <channel>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8090"
ROOT = Path.home() / "ambient-streamer"
TOKEN = ""


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def relay_paths() -> dict:
    out = subprocess.run(
        ["docker", "run", "--rm", "--network", "ambient", "--entrypoint", "python3",
         "ambient-composer:dev", "-c",
         "import urllib.request,sys;"
         "sys.stdout.write(urllib.request.urlopen("
         "'http://mediamtx:9997/v3/paths/list', timeout=10).read().decode())"],
        capture_output=True, text=True, timeout=90).stdout
    try:
        return {item["name"]: item for item in json.loads(out).get("items", [])}
    except Exception:
        return {}


def main() -> int:
    global TOKEN
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip().strip('"')

    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<50} {note}")
        if not ok:
            failures.append(label)

    code, status = call("GET", f"/api/channels/{channel}")
    if code != 200:
        print(f"cannot read {channel}: {code} {status}")
        return 2
    if status.get("youtube") is not False:
        print(f"{channel} is not an internal channel; set CHANNEL_PUBLISH_YOUTUBE=false")
        return 2

    print(f"{channel}: local rendition {status.get('local')}\n")

    paths = relay_paths()
    check("the relay has no program path for it", channel not in paths,
          ", ".join(k for k in paths if k.startswith(channel)) or "none")
    preview = paths.get(f"{channel}/preview")
    check("the local path is publishing", bool(preview and preview.get("ready")),
          f"tracks={len((preview or {}).get('tracks', []))}")
    check("status says local-only, not disconnected",
          status.get("rtmp") == "local-only", str(status.get("rtmp")))
    check("HLS is up", status.get("hls") == "ok", str(status.get("hls")))

    # No publisher process can exist without a program path to hang it on.
    procs = subprocess.run(
        ["docker", "exec", "ambient-mediamtx", "sh", "-c",
         "ps -o args= 2>/dev/null | grep -c '[p]ublish-youtube.sh' || true"],
        capture_output=True, text=True).stdout.strip()
    hooks = subprocess.run(
        ["docker", "logs", "--since", "10m", "ambient-mediamtx"],
        capture_output=True, text=True)
    # `[publish <channel>]` is publish-youtube.sh's own log prefix. Matching on
    # the word "publish" alone catches the composer publishing to the LOCAL
    # path, which is the thing that is supposed to be happening.
    marker = f"[publish {channel}]"
    mentions = [
        line for line in (hooks.stdout + hooks.stderr).splitlines() if marker in line
    ]
    check("no YouTube publisher was started for it", not mentions and procs == "0",
          f"{procs} running, {len(mentions)} hook log lines")

    # The whole point: a stream key present in .env must change nothing.
    env_path = ROOT / "channels" / channel / ".env"
    has_key = any(
        line.startswith("YOUTUBE_STREAM_KEY=") and line.split("=", 1)[1].strip()
        for line in env_path.read_text().splitlines()
    )
    print(f"\n  (stream key {'present' if has_key else 'absent'} in .env "
          f"\u2014 it is not what keeps this channel off YouTube)")

    # And it still has to actually be a stream.
    _, again = call("GET", f"/api/channels/{channel}")
    time.sleep(6)
    _, later = call("GET", f"/api/channels/{channel}")
    moved = later.get("uptime_seconds", 0) > again.get("uptime_seconds", 0)
    check("the compositor is making progress", moved,
          f"{again.get('uptime_seconds')} -> {later.get('uptime_seconds')}")
    check("it is holding realtime", (later.get("speed") or 0) >= 0.97,
          f"{later.get('speed')}x at {later.get('fps')} fps")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("internal channel: serving HLS locally, no YouTube leg at all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
