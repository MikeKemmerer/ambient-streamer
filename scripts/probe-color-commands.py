#!/usr/bin/env python3
"""Show what the color API emits and whether it reaches the filtergraph."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
CHANNEL = "vibecoding"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    accent = sys.argv[1] if len(sys.argv) > 1 else "#FF3B30"
    body = json.dumps({"mode": "manual", "manual": {"accent": accent, "tint": "#2A0805"}}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:8090/api/channels/{CHANNEL}/color",
        data=body, method="PUT",
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read())

    print(f"requested accent : {accent}")
    print(f"baked accent     : {(ROOT / '.run' / CHANNEL / 'viz-accent').read_text().strip()}")
    print("commands emitted :")
    for message in data.get("commands") or []:
        print(f"   {message}")
    if not data.get("commands"):
        print("   (none)")

    box = subprocess.run(
        ["docker", "ps", "--filter", f"name={CHANNEL}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()[0]
    errors = subprocess.run(
        ["docker", "logs", "ambient-backend", "--since", "60s"],
        capture_output=True, text=True,
    )
    swallowed = [l for l in (errors.stdout + errors.stderr).splitlines() if "zmq" in l.lower() or "color" in l.lower()]
    print(f"backend notes    : {swallowed[-3:] if swallowed else 'none'}")
    print(f"composer         : {box}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
