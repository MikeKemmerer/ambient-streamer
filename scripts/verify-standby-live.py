#!/usr/bin/env python3
"""Standby and plugin parameters, on a live channel.

The claim is that the visualization can be taken off air and put back with no
restart. A restart would also make the picture change, so this checks the
composer's PID and uptime either side - if the container was replaced, the
result proves nothing.
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


TOKEN = token()


def call(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def composer(name: str) -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    return out[0] if out else ""


def started_at(name: str) -> str:
    box = composer(name)
    if not box:
        return ""
    return subprocess.run(
        ["docker", "inspect", box, "--format", "{{.State.StartedAt}}"],
        capture_output=True, text=True).stdout.strip()


def graph(name: str) -> str:
    return (ROOT / ".run" / name / "filtergraph.txt").read_text(encoding="utf-8")


def speed(name: str) -> str:
    text = (ROOT / ".run" / name / "progress").read_bytes()[-3000:].decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("speed="):
            return line.split("=", 1)[1].strip()
    return "?"


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "westcoastclassics"
    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<46} {note}")
        if not ok:
            failures.append(label)

    print(f"enabling the visualization on {name} and restarting")
    call(f"/api/channels/{name}", "PATCH", {"visualization": {"enabled": True}})
    call(f"/api/channels/{name}/visualization/parameters", "PUT",
         {"plugin": "showfreqs-bars", "values": {"detail": 2048, "smoothing": 6, "shape": "line"}})
    call(f"/api/channels/{name}/restart", "POST", {})
    time.sleep(75)

    running = graph(name)
    check("overlay carries a timeline switch", "overlay@viz" in running and "enable=1" in running)
    check("plugin parameters were substituted",
          "win_size=2048" in running and "averaging=6" in running and "mode=line" in running,
          "win_size=2048 averaging=6 mode=line")
    check("no token survived into the graph", "${" not in running)
    check("holding realtime", speed(name) >= "0.95", f"speed={speed(name)}")

    before_start = started_at(name)
    print(f"\n  composer started at {before_start}; going to standby")
    status, body = call(f"/api/channels/{name}/visualization/visible", "PUT", {"visible": False})
    check("standby accepted", status == 202, str(body.get("detail", body))[:70])
    check("reported as live", bool(body.get("live")), "no restart claimed")
    time.sleep(6)

    check("composer was NOT replaced", started_at(name) == before_start,
          "same container, so the change really was live")
    check("still realtime after standby", speed(name) >= "0.95", f"speed={speed(name)}")

    print("\n  back on air")
    status, body = call(f"/api/channels/{name}/visualization/visible", "PUT", {"visible": True})
    check("restored", status == 202 and bool(body.get("live")))
    time.sleep(6)
    check("composer still not replaced", started_at(name) == before_start)
    check("still realtime", speed(name) >= "0.95", f"speed={speed(name)}")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("standby toggles the visualization on a running graph, no restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
