#!/usr/bin/env python3
"""Exercise the ingest settings endpoint against the running control plane.

The stream key is the reason this is worth checking live rather than only in
tests: it is write-only, and a leak would be into a file, a log or an argv that
the test fixtures do not model.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"
STOPPED = "westcoastclassics"
RUNNING = "vibecoding"
SECRET = "zzzz-test-key-9999"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<46} {note}")
        if not ok:
            failures.append(label)

    print("ingest settings on a stopped channel")

    env_path = ROOT / "channels" / STOPPED / ".env"
    before = env_path.read_text(encoding="utf-8")
    original_key = ""
    for line in before.splitlines():
        if line.startswith("YOUTUBE_STREAM_KEY="):
            original_key = line.split("=", 1)[1]

    status, body = call(f"/api/channels/{STOPPED}/delivery", "PUT",
                        {"fps": 24, "encoder": "libx264"})
    check("stopped channel accepts a change", status == 200, f"HTTP {status} {body.get('changed')}")

    status, detail = call(f"/api/channels/{STOPPED}")
    check("fps_requested reflects it", detail.get("fps_requested") == 24, str(detail.get("fps_requested")))
    check("measured fps is separate", detail.get("fps") == 0.0, str(detail.get("fps")))

    status, body = call(f"/api/channels/{STOPPED}/delivery", "PUT", {"stream_key": SECRET})
    check("key accepted", status == 200 and body.get("has_stream_key") is True, f"HTTP {status}")
    check("key not echoed", SECRET not in json.dumps(body), "absent from response")

    status, detail = call(f"/api/channels/{STOPPED}")
    check("key not in channel detail", SECRET not in json.dumps(detail), "absent")
    status, listing = call("/api/channels")
    check("key not in channel list", SECRET not in json.dumps(listing), "absent")

    logs = subprocess.run(["docker", "logs", "ambient-backend", "--since", "3m"],
                          capture_output=True, text=True)
    check("key not in backend logs", SECRET not in (logs.stdout + logs.stderr), "absent")

    inspect = subprocess.run(
        ["docker", "inspect", "ambient-mediamtx", "ambient-backend"],
        capture_output=True, text=True)
    check("key not in docker inspect", SECRET not in inspect.stdout, "absent")

    perms = oct(env_path.stat().st_mode & 0o777)
    check("channel .env stays 0600", perms == "0o600", perms)

    status, body = call(f"/api/channels/{STOPPED}/delivery", "PUT",
                        {"rtmp_url": "http://evil.example/live"})
    check("hostile URL refused", status == 400, f"HTTP {status} {body.get('error')}")

    status, body = call(f"/api/channels/{RUNNING}/delivery", "PUT", {"fps": 24})
    check("running channel refused", status == 409 and body.get("error") == "channel_running",
          f"HTTP {status} {body.get('error')}")

    # Put the channel back exactly as it was.
    call(f"/api/channels/{STOPPED}/delivery", "PUT",
         {"stream_key": original_key, "clear_fps": True, "clear_encoder": True})
    print(f"\n  restored {STOPPED} (key length {len(original_key)}, fps/encoder back to default)")

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nsettings apply, and the key stays where it belongs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
