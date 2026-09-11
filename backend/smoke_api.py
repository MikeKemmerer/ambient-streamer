#!/usr/bin/env python3
"""Throwaway smoke test: start the real server, hit every route over HTTP.

Not part of the suite. Run with the backend venv from the repo root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "smoke-token-0123456789abcdef"
PORT = 8391
BASE = f"http://127.0.0.1:{PORT}"


def build_repo(root: Path) -> Path:
    (root / "common" / "audio").mkdir(parents=True)
    (root / "common" / "images").mkdir(parents=True)
    channel = root / "channels" / "lofi"
    (channel / "audio").mkdir(parents=True)
    (channel / "images").mkdir(parents=True)
    (channel / "audio" / "01 - a track.m4a").write_bytes(b"x")
    (channel / "images" / "slide.jpg").write_bytes(b"x")
    (channel / "config.yaml").write_text(
        "version: 1\nname: lofi\naudio:\n  tracks: []\nimages:\n  slides: []\n"
        "visualization:\n  active: showfreqs-bars\n  hot_set: [showfreqs-bars]\n"
        "schedule:\n  timezone: America/Los_Angeles\n  rules: []\n",
        encoding="utf-8",
    )
    (channel / ".env").write_text(
        "YOUTUBE_STREAM_KEY=aaaa-bbbb-cccc-dddd-eeee\nCHANNEL_MOUNT=/lofi\n"
        "CHANNEL_FALLBACK_MOUNT=/lofi-fallback\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(f"AMBIENT_LOG_DIR={root / 'logs'}\n", encoding="utf-8")
    (root / "docker").mkdir()
    shutil.copy(ROOT / "docker" / "compose.channel.yml.j2", root / "docker")
    shutil.copytree(ROOT / "plugins", root / "plugins")
    (root / "presets").mkdir()
    (root / "presets" / "calm-ocean.yaml").write_text(
        "version: 1\nname: calm-ocean\ndisplay_name: Calm Ocean\n"
        "visualization:\n  active: showfreqs-bars\n"
        "color:\n  mode: manual\n  manual:\n    accent: '#4FC3F7'\n    tint: '#0B2A3A'\n"
        "  transition_seconds: 4.0\nslideshow:\n  hold_seconds: 30.0\n  fade_seconds: 3.0\n",
        encoding="utf-8",
    )
    (channel / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return root


def request(method: str, path: str, body=None, token: str | None = TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


CASES = [
    ("GET", "/api/health", None, 200, None),
    # The operator UI: unauthenticated on purpose — a browser cannot attach a
    # bearer token to the navigation that fetches the page.
    ("GET", "/", None, 200, None),
    ("GET", "/style.css", None, 200, None),
    ("GET", "/app.js", None, 200, None),
    ("GET", "/nope.js", None, 404, None),
    ("GET", "/%2e%2e%2f.env", None, 404, None),
    ("GET", "/api/channels", None, 401, None),
    ("GET", "/api/channels", None, 200, TOKEN),
    ("GET", "/api/channels/lofi", None, 200, TOKEN),
    ("GET", "/api/channels/nope", None, 404, TOKEN),
    ("GET", "/api/channels/UPPER", None, 400, TOKEN),
    ("GET", "/api/system", None, 200, TOKEN),
    ("GET", "/api/system?refresh=true", None, 200, TOKEN),
    ("GET", "/api/capacity", None, 200, TOKEN),
    ("GET", "/api/plugins", None, 200, TOKEN),
    ("GET", "/api/presets", None, 200, TOKEN),
    ("GET", "/api/metrics", None, 200, TOKEN),
    ("GET", "/api/media/audio", None, 200, TOKEN),
    ("GET", "/api/media/images", None, 200, TOKEN),
    ("POST", "/api/media/profiles", {}, 202, TOKEN),
    # Upload takes multipart only; a JSON body proves the routes are there and
    # refuse anything else. The multipart paths are covered in test_uploads.py.
    ("POST", "/api/media/upload", {}, 400, TOKEN),
    ("POST", "/api/media/audio/upload", {}, 400, TOKEN),
    ("POST", "/api/media/upload", {}, 401, None),
    ("GET", "/api/logs?channel=lofi&service=compositor", None, 200, TOKEN),
    ("GET", "/api/logs?channel=lofi&service=liquidsoap", None, 200, TOKEN),
    ("GET", "/api/logs?channel=lofi&service=producer", None, 200, TOKEN),
    ("GET", "/api/logs?channel=lofi&service=watchdog", None, 200, TOKEN),
    ("GET", "/api/logs?channel=lofi&service=../etc", None, 400, TOKEN),
    ("GET", "/api/channels/lofi/playlist", None, 200, TOKEN),
    ("PUT", "/api/channels/lofi/playlist", {"tracks": ["channels/lofi/audio/01 - a track.m4a"]}, 200, TOKEN),
    ("PUT", "/api/channels/lofi/playlist", {"tracks": ["../../etc/passwd"]}, 400, TOKEN),
    ("GET", "/api/channels/lofi/images", None, 200, TOKEN),
    ("GET", "/api/channels/lofi/soundboard", None, 200, TOKEN),
    ("PUT", "/api/channels/lofi/images", {"slides": ["channels/lofi/images/*"]}, 200, TOKEN),
    ("PUT", "/api/channels/lofi/visualization", {"active": "showfreqs-bars"}, 200, TOKEN),
    ("PUT", "/api/channels/lofi/visualization?allow_restart=false", {"active": "minimal-line"}, 409, TOKEN),
    ("PUT", "/api/channels/lofi/visualization", {"active": "minimal-line"}, 202, TOKEN),
    ("PUT", "/api/channels/lofi/visualization", {"active": "nope"}, 404, TOKEN),
    ("POST", "/api/channels/lofi/preset", {"preset": "calm-ocean"}, 202, TOKEN),
    ("POST", "/api/channels/lofi/preset", {"preset": "../etc"}, 400, TOKEN),
    ("PUT", "/api/channels/lofi/color", {"mode": "manual", "manual": {"accent": "#4FC3F7", "tint": "#101820"}}, 200, TOKEN),
    ("GET", "/api/channels/lofi/bumpers", None, 200, TOKEN),
    ("PUT", "/api/channels/lofi/bumpers", {"every_tracks": 3, "text": {"bumpers": [{"id": "station-id", "text": "hi"}]}}, 200, TOKEN),
    ("POST", "/api/channels/lofi/bumpers/generate", None, 202, TOKEN),
    ("GET", "/api/channels/lofi/bumpers/station-id/preview", None, 404, TOKEN),
    ("GET", "/api/channels/lofi/preview", None, 200, TOKEN),
    ("POST", "/api/channels", {"name": "rain", "stream_key": "zzzz-yyyy"}, 201, TOKEN),
    ("POST", "/api/channels", {"name": "rain"}, 409, TOKEN),
    ("PATCH", "/api/channels/lofi", {"genre": "sleep"}, 200, TOKEN),
    ("PATCH", "/api/channels/lofi", {"resolution": "1080p"}, 400, TOKEN),
    ("PUT", "/api/channels/lofi/resolution", {"resolution": "480p"}, 202, TOKEN),
    ("PUT", "/api/channels/lofi/resolution", {"resolution": "8k"}, 400, TOKEN),
    ("POST", "/api/channels/lofi/start", None, 202, TOKEN),
    ("POST", "/api/channels/lofi/stop", None, 202, TOKEN),
    ("POST", "/api/channels/lofi/restart", None, 202, TOKEN),
    ("DELETE", "/api/channels/rain", None, 200, TOKEN),
]


def sse_probe() -> tuple[int, str]:
    req = urllib.request.Request(BASE + "/api/events")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as response:
        first = response.readline().decode()
        return response.status, first


def frontend_probe() -> tuple[int, str, bool]:
    req = urllib.request.Request(BASE + "/")
    with urllib.request.urlopen(req, timeout=10) as response:
        body = response.read().decode("utf-8", "replace")
        ctype = response.headers.get("Content-Type", "")
        return response.status, ctype, "<!DOCTYPE html>" in body


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ambient-smoke-"))
    build_repo(workdir)
    env = {
        **os.environ,
        "AMBIENT_API_TOKEN": TOKEN,
        "AMBIENT_BIND_ADDRESS": "127.0.0.1",
        "AMBIENT_ROOT": str(workdir),
        # The repo's own frontend/, not the baked image path, which is absent here.
        "AMBIENT_FRONTEND_DIR": str(ROOT / "frontend"),
        "PATH": os.environ["PATH"] + ":" + str(workdir / "bin"),
    }
    # A stub `docker` so lifecycle calls have something to talk to.
    (workdir / "bin").mkdir()
    stub = workdir / "bin" / "docker"
    stub.write_text("#!/bin/sh\necho \"$@\" >> \"$0.log\"\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    server = subprocess.Popen(
        [sys.executable, "-m", "ambient.main", "--repo-root", str(workdir), "--port", str(PORT)],
        cwd=str(ROOT / "backend"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(100):
            try:
                if request("GET", "/api/health", token=None)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("server never came up")

        failures = 0
        for method, path, body, expected, token in CASES:
            status, payload = request(method, path, body, token)
            ok = status == expected
            failures += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} {status:>3} (want {expected:>3}) {method:6} {path}")
            if not ok:
                print(f"       {payload[:300]}")
            if "stream_key" in path or "rain" in path:
                assert "zzzz-yyyy" not in payload, "a stream key was echoed back"

        status, first = sse_probe()
        sse_ok = status == 200 and first.startswith(":")
        failures += 0 if sse_ok else 1
        print(f"{'ok  ' if sse_ok else 'FAIL'} {status:>3} (want 200) GET    /api/events -> {first!r}")

        status, ctype, is_html = frontend_probe()
        ui_ok = status == 200 and ctype.startswith("text/html") and is_html
        failures += 0 if ui_ok else 1
        print(f"{'ok  ' if ui_ok else 'FAIL'} {status:>3} (want 200) GET    / -> {ctype!r} html={is_html}")

        print(f"\n{len(CASES) + 2} requests, {failures} failure(s)")
        return 1 if failures else 0
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            out = ""
        if TOKEN in out or "aaaa-bbbb" in out or "zzzz-yyyy" in out:
            print("FAIL: a secret appeared in the server log")
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
