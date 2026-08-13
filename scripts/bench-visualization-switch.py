#!/usr/bin/env python3
"""A/B the visualization switch on a live channel.

Sets the state explicitly in both directions and restarts each time, because a
reading taken without a restart measures whatever graph happens to be running,
not the config. Averages over a full slideshow cycle: crossfades swing a single
`docker stats` sample by tens of percent.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"
SETTLE = 75
SAMPLES = 14
GAP = 5


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = token()


def call(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read() or b"{}")


def composer(name: str) -> str:
    names = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    return names[0] if names else ""


def sample(name: str) -> tuple[float, float]:
    """Mean and standard deviation of composer CPU, in cores."""
    values: list[float] = []
    for _ in range(SAMPLES):
        box = composer(name)
        if box:
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", box],
                capture_output=True, text=True).stdout.strip().rstrip("%")
            try:
                values.append(float(out) / 100.0)
            except ValueError:
                pass
        time.sleep(GAP)
    if not values:
        return -1.0, 0.0
    return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def speed(name: str) -> float:
    text = (ROOT / ".run" / name / "progress").read_bytes()[-3000:].decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("speed="):
            try:
                return float(line.split("=", 1)[1].strip().rstrip("x"))
            except ValueError:
                return 0.0
    return 0.0


def graph_has_viz(name: str) -> bool:
    text = (ROOT / ".run" / name / "filtergraph.txt").read_text(encoding="utf-8")
    return "alphamerge" in text or "streamselect" in text


def phase(name: str, enabled: bool) -> tuple[float, float, float]:
    call(f"/api/channels/{name}", "PATCH", {"visualization": {"enabled": enabled}})
    call(f"/api/channels/{name}/restart", "POST", {})
    time.sleep(SETTLE)
    detail = call(f"/api/channels/{name}")
    got = graph_has_viz(name)
    label = "on" if enabled else "off"
    print(f"  visualization {label}: graph has viz branches = {got} "
          f"(projected {detail.get('projected_cores')})")
    if got is not enabled:
        print(f"  ABORT: asked for {label} but the running graph disagrees")
        sys.exit(1)
    mean, dev = sample(name)
    rate = speed(name)
    # A channel that cannot keep up is doing less work per wall second, so raw
    # CPU understates it. Normalising gives the cost of one second of stream.
    per_realtime = mean / rate if rate > 0 else float("inf")
    print(f"  visualization {label}: {mean:.2f} cores (sd {dev:.2f}) at {rate:.3f}x "
          f"= {per_realtime:.2f} cores per realtime second")
    return mean, dev, per_realtime


def main() -> int:
    # No default: this restarts the channel repeatedly.
    if len(sys.argv) < 2:
        print("usage: bench-visualization-switch.py <channel>")
        return 2
    name = sys.argv[1]
    before = call(f"/api/channels/{name}")
    original = (before.get("config") or {}).get("visualization", {}).get("enabled", True)
    res = before.get("resolution")
    print(f"A/B on {name} ({res}, fps {before.get('fps_requested')}), "
          f"restoring enabled={original} at the end\n")

    _, _, on_cost = phase(name, True)
    _, _, off_cost = phase(name, False)

    print()
    if on_cost == float("inf"):
        print("visualization on could not encode at all on this host")
        verdict = 0
    else:
        saved = on_cost - off_cost
        print(f"visualization off costs {off_cost:.2f} cores per realtime second "
              f"against {on_cost:.2f} on: a saving of {saved:.2f} "
              f"({saved / on_cost * 100:.0f}%)")
        verdict = 0 if saved > 0 else 1

    if original:
        print("restoring the original setting")
        phase(name, True)
    return verdict


if __name__ == "__main__":
    sys.exit(main())
