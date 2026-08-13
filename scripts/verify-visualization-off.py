#!/usr/bin/env python3
"""Check that turning the visualization off produces the graph it should.

Correctness only. The CPU saving is a separate claim, measured by
bench-visualization-switch.py, which drives both directions and averages over a
full slideshow cycle. A before/after pair taken here would be sampling noise
dressed up as evidence: crossfades move a single reading by tens of percent.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def call(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read() or b"{}")


def composer(name: str) -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    return out[0] if out else ""


def speed(name: str) -> str:
    text = (ROOT / ".run" / name / "progress").read_bytes()[-3000:].decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("speed="):
            return line.split("=", 1)[1].strip()
    return "?"


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "kemmicalrhythmz"
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    # Start from a known on state rather than whatever the last run left behind.
    call(f"/api/channels/{name}", "PATCH", {"visualization": {"enabled": True}})
    on = call(f"/api/channels/{name}")
    was_active = (on.get("config") or {}).get("visualization", {}).get("active")
    on_cores = on.get("projected_cores", 0)
    print(f"{name}: {on.get('resolution')} @ {on.get('fps_requested')}fps, "
          f"plugin {was_active}, projected {on_cores}\n")

    call(f"/api/channels/{name}", "PATCH", {"visualization": {"enabled": False}})
    off = call(f"/api/channels/{name}")
    viz = (off.get("config") or {}).get("visualization") or {}
    check("saved as disabled", viz.get("enabled") is False, str(viz.get("enabled")))
    check("plugin choice preserved", viz.get("active") == was_active, str(viz.get("active")))
    check("hot set preserved", bool(viz.get("hot_set")), str(viz.get("hot_set")))
    check("projection dropped", off.get("projected_cores", 9) < on_cores,
          f"{on_cores} -> {off.get('projected_cores')}")

    call(f"/api/channels/{name}/restart", "POST", {})
    print("  restarted, settling...")
    time.sleep(70)

    logs = subprocess.run(["docker", "logs", composer(name)], capture_output=True, text=True)
    text = logs.stdout + logs.stderr
    check("composer announces it", "visualization: off" in text, "logged")
    check("no ffmpeg filter error",
          "Error initializing" not in text and "Invalid argument" not in text, "clean")

    graph = (ROOT / ".run" / name / "filtergraph.txt").read_text(encoding="utf-8")
    check("no plugin filter in the graph",
          not any(p in graph for p in ("showfreqs", "showwaves", "avectorscope", "aphasemeter")),
          "absent")
    check("no branch selector", "streamselect" not in graph, "absent")
    check("no alpha composite", "alphamerge" not in graph and "lut@vizop" not in graph, "absent")
    check("colour control survives", "zmq@ctl" in graph and "hue@hue" in graph, "present")
    check("still encodes and previews", "split=2[vmain][vpre]" in graph, "present")
    check("holding realtime", speed(name) >= "0.95", f"speed={speed(name)}")

    print("\n  restoring the visualization")
    call(f"/api/channels/{name}", "PATCH", {"visualization": {"enabled": True}})
    call(f"/api/channels/{name}/restart", "POST", {})
    time.sleep(70)
    back = (ROOT / ".run" / name / "filtergraph.txt").read_text(encoding="utf-8")
    check("switching back restores the branches",
          "streamselect" in back or "alphamerge" in back, "present")
    restored = (call(f"/api/channels/{name}").get("config") or {}).get("visualization", {})
    check("same plugin came back", restored.get("active") == was_active, str(was_active))

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("visualization off and back on is correct end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
