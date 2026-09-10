#!/usr/bin/env python3
"""Fetch the operator preview the way the browser does, and play a segment.

The UI asks this process for /<channel>/preview/index.m3u8; the relay publishes
no host port and its internal address resolves nowhere in a browser, so the
proxy is the only path that can work. A playlist alone proves little - the
segments it names have to come back too, and decode.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
BASE = "http://127.0.0.1:8090"
CHANNEL = "vibecoding"


def token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def get(path: str, auth: bool = True) -> tuple[int, bytes, str]:
    headers = {"Authorization": "Bearer " + token()} if auth else {}
    request = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", ""
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return 0, str(exc).encode(), ""


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    print("the operator preview must play through the control plane")

    status, body, _ = get(f"/api/channels/{CHANNEL}/preview")
    advertised = json.loads(body)["hls"] if status == 200 else ""
    check("advertised URL is browser-reachable",
          advertised.startswith("/") and "mediamtx" not in advertised, advertised)

    status, playlist, ctype = get(f"/{CHANNEL}/preview/index.m3u8")
    check("playlist proxied", status == 200 and playlist.startswith(b"#EXTM3U"), f"HTTP {status}")
    check("served as HLS", "mpegurl" in ctype.lower(), ctype)

    status, _, _ = get(f"/{CHANNEL}/preview/index.m3u8", auth=False)
    check("preview needs the token", status == 401, f"HTTP {status}")

    lines = [l.strip() for l in playlist.decode(errors="replace").splitlines()]
    refs = [l for l in lines if l and not l.startswith("#")]
    check("playlist names something to fetch", bool(refs), f"{len(refs)} entries")
    if not refs:
        return 1

    # A variant playlist points at another playlist; follow one level.
    target = refs[0]
    if target.endswith(".m3u8"):
        status, playlist, _ = get(f"/{CHANNEL}/preview/{target}")
        check("variant playlist proxied", status == 200, f"HTTP {status} {target}")
        lines = [l.strip() for l in playlist.decode(errors="replace").splitlines()]
        refs = [l for l in lines if l and not l.startswith("#")]
        prefix = target.rsplit("/", 1)[0] + "/" if "/" in target else ""
    else:
        prefix = ""

    work = Path(tempfile.mkdtemp(prefix="preview-"))
    fetched = 0
    blob = b""
    for ref in refs[:3]:
        status, chunk, _ = get(f"/{CHANNEL}/preview/{prefix}{ref}")
        if status == 200 and chunk:
            fetched += 1
            blob += chunk
    check("segments proxied", fetched > 0, f"{fetched} of {min(3, len(refs))}")

    if blob:
        sample = work / "sample.mp4"
        sample.write_bytes(blob)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", str(sample)],
            capture_output=True, text=True,
        )
        streams = probe.stdout.strip().replace("\n", " ")
        check("the bytes decode as media", bool(streams), streams or probe.stderr.strip()[:80])

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the preview is reachable, authenticated and decodable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
