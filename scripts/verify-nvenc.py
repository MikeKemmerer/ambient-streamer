#!/usr/bin/env python3
"""Switch a channel to NVENC and prove the encode really moved to the GPU.

NVENC can fail open in two directions: the container can start with the encoder
silently unavailable, or ffmpeg can be asked for nvenc and still burn CPU. So
this checks the GPU's own encoder session counter and the CPU drop together,
not just that the channel came back up.
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


def call(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def gpu() -> tuple[int, int]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=utilization.encoder,memory.used",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    ).stdout.strip()
    enc, mem = (int(x.strip()) for x in out.split(","))
    return enc, mem


def cpu(name: str) -> float:
    box = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()
    if not box:
        return -1.0
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", box[0]],
        capture_output=True, text=True,
    ).stdout.strip().rstrip("%")
    try:
        return float(out)
    except ValueError:
        return -1.0


def speed(name: str) -> str:
    text = (ROOT / ".run" / name / "progress").read_bytes()[-3000:].decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("speed="):
            return line.split("=", 1)[1]
    return "?"


def main() -> int:
    # No default: this restarts the channel.
    if len(sys.argv) < 2:
        print("usage: verify-nvenc.py <channel>")
        return 2
    name = sys.argv[1]
    encoder = sys.argv[2] if len(sys.argv) > 2 else "h264_nvenc"
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    before_cpu = cpu(name)
    before_enc, _ = gpu()
    print(f"switching {name} to {encoder}")
    print(f"  baseline: cpu {before_cpu:.0f}%  gpu-encoder {before_enc}%")

    call(f"/api/channels/{name}/stop", "POST", {})
    for _ in range(30):
        time.sleep(2)
        if not subprocess.run(
            ["docker", "ps", "--filter", f"name={name}-composer", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        ).stdout.strip():
            break

    status, body = call(f"/api/channels/{name}/delivery", "PUT", {"encoder": encoder})
    check("encoder accepted while stopped", status == 200, f"HTTP {status} {body.get('encoder')}")

    status, body = call(f"/api/channels/{name}/start", "POST", {})
    check("channel started", status in (200, 202), f"HTTP {status}")

    print("  settling 60s...")
    time.sleep(60)

    detail = call(f"/api/channels/{name}")[1]
    check("state running", detail.get("state") == "running", str(detail.get("state")))
    check("encoder in effect", detail.get("encoder") == encoder,
          f"{detail.get('encoder')} (requested {detail.get('encoder_requested')})")

    after_enc, after_mem = gpu()
    after_cpu = cpu(name)
    check("GPU encoder is actually busy", after_enc > 0, f"{after_enc}% encoder, {after_mem} MiB")
    check("CPU dropped", after_cpu < before_cpu, f"{before_cpu:.0f}% -> {after_cpu:.0f}%")

    now = speed(name)
    check("holding realtime", now.strip().rstrip("x") >= "0.95", f"speed={now}")

    logs = subprocess.run(
        ["docker", "logs", f"{name}-composer", "--tail", "200"],
        capture_output=True, text=True,
    )
    text = logs.stdout + logs.stderr
    check("no nvenc fallback in logs",
          "Cannot load libnvidia-encode" not in text and "Nvenc unloaded" not in text,
          "clean")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print(f"{name} is encoding on the GPU")
    return 0


if __name__ == "__main__":
    sys.exit(main())
