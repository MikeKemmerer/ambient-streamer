#!/usr/bin/env python3
"""Prove an uploaded track enters rotation without restarting the stream.

The whole point of the upload feature is that new media reaches a 24/7 broadcast
live. So this checks two things that must both hold: the playlist grew, and
nothing restarted to make that happen.
"""
from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import wave

BASE = "http://127.0.0.1:8090"
CHANNEL = "vibecoding"
NAME = "upload-probe.wav"
ROOT = os.path.expanduser("~/ambient-streamer")
PLAYLIST = os.path.join(ROOT, "channels", CHANNEL, "playlist.m3u")


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def tracks() -> int:
    with open(PLAYLIST, encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip() and not line.startswith("#"))


def tone() -> bytes:
    buf = io.BytesIO()
    writer = wave.open(buf, "wb")
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(8000)
    writer.writeframes(struct.pack("<8000h", *([0] * 8000)))
    writer.close()
    return buf.getvalue()


def upload(token: str, payload: bytes) -> tuple[int, dict]:
    boundary = "----x%s" % uuid.uuid4().hex
    body = b""
    # file first, then the naming fields - the order a browser actually sends
    body += (
        '--%s\r\nContent-Disposition: form-data; name="files"; filename="%s"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n" % (boundary, NAME)
    ).encode()
    body += payload + b"\r\n"
    for key, value in (("destination", "channel"), ("channel", CHANNEL), ("kind", "audio")):
        body += (
            '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, key, value)
        ).encode()
    body += ("--%s--\r\n" % boundary).encode()

    request = urllib.request.Request(
        BASE + "/api/media/upload",
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    token = ""
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("AMBIENT_API_TOKEN="):
                token = line.split("=", 1)[1].strip()

    before_tracks = tracks()
    before_pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", f"{CHANNEL}-composer")
    before_ls = sh("docker", "inspect", "-f", "{{.State.Pid}}", f"{CHANNEL}-liquidsoap")

    status, payload = upload(token, tone())

    after_tracks = tracks()
    after_pid = sh("docker", "inspect", "-f", "{{.State.Pid}}", f"{CHANNEL}-composer")
    after_ls = sh("docker", "inspect", "-f", "{{.State.Pid}}", f"{CHANNEL}-liquidsoap")

    failures = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<40} {note}")
        if not ok:
            failures.append(label)

    print("upload reaches the live stream")
    check("upload accepted", status == 200, f"HTTP {status}")
    check("playlist grew by one", after_tracks == before_tracks + 1, f"{before_tracks} -> {after_tracks}")
    check("recompile reported", CHANNEL in (payload.get("recompiled") or []), str(payload.get("recompiled")))
    check("composer never restarted", before_pid == after_pid and before_pid != "", f"pid {before_pid}")
    check("liquidsoap never restarted", before_ls == after_ls and before_ls != "", f"pid {before_ls}")

    # put the channel back exactly as it was
    stored = os.path.join(ROOT, "channels", CHANNEL, "audio", NAME)
    if os.path.exists(stored):
        os.replace(stored, "/tmp/" + NAME)
    print()
    print(f"  cleaned up, playlist back to {before_tracks} after next recompile")

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nnew audio entered rotation with nothing restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
