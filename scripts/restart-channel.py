#!/usr/bin/env python3
"""Restart a channel through the API and report what the composer ended up with.

A composer restart costs a real gap and a new YouTube ingest session, so it is
worth confirming the values it restarted for actually arrived.
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
WANTED = (
    "COLOR_MODE",
    "COLOR_ACCENT",
    "COLOR_TINT",
    "COLOR_TRANSITION_SECONDS",
    "COLOR_INIT_HUE",
    "COLOR_INIT_SATURATION",
    "COLOR_INIT_BRIGHTNESS",
    "WIDTH",
    "HEIGHT",
    "ACTIVE_PLUGIN",
)


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
    request = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def env_of(container: str) -> dict[str, str]:
    out = subprocess.run(
        ["docker", "inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
        capture_output=True,
        text=True,
    ).stdout
    pairs = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


def main() -> int:
    channel = sys.argv[1] if len(sys.argv) > 1 else "vibecoding"
    status, text = call(f"/api/channels/{channel}/restart", "POST", b"{}")
    print(f"restart -> HTTP {status} {text[:160]}")

    compose = ROOT / "channels" / channel / "docker-compose.yml"
    print(f"compose regenerated with COLOR_MODE: {'COLOR_MODE' in compose.read_text(encoding='utf-8')}")

    for _ in range(30):
        time.sleep(2)
        env = env_of(f"{channel}-composer")
        if "COLOR_MODE" in env:
            break

    print("\ncomposer environment:")
    missing = []
    for key in WANTED:
        value = env.get(key)
        print(f"  {'ok  ' if value is not None else 'MISS'} {key:<26} {value!r}")
        if value is None:
            missing.append(key)

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
