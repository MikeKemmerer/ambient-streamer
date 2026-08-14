#!/usr/bin/env python3
"""Is anything in the current audio graph seekable?

The earlier "no seek" finding predates the request queue. A `request.queue`
plays a file, and file decoders can seek, so the answer may have changed for a
pushed track even though it did not for the playlist.

`icecast.seek` acts on the OUTPUT's source, which is the outermost operator, so
what this really measures is whether seek survives crossfade and mksafe.

Usage: probe-seek.py <channel>
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
CURRENT = re.compile(r"current='([^']*)'")
ELAPSED = re.compile(r"elapsed=([0-9.]+)")


def telnet(channel: str, *commands: str) -> str:
    script = "\n".join([*commands, "quit", ""])
    inner = (
        "import socket, sys\n"
        f's = socket.create_connection(("{channel}-liquidsoap", 1234), timeout=10)\n'
        f"s.sendall({script!r}.encode())\n"
        'buf = b""\n'
        "s.settimeout(3)\n"
        "try:\n"
        "    while True:\n"
        "        c = s.recv(4096)\n"
        "        if not c: break\n"
        "        buf += c\n"
        "except Exception: pass\n"
        'sys.stdout.write(buf.decode("utf-8", "replace"))\n'
    )
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "ambient", "--entrypoint",
         "python3", "ambient-composer:dev", "-c", inner],
        capture_output=True, text=True, timeout=90).stdout


def clean(text: str) -> str:
    return " ".join(
        l.strip() for l in text.splitlines()
        if l.strip() and l.strip() not in {"END", "Bye!"}
    )


def status(channel: str) -> tuple[str, float]:
    text = telnet(channel, "ambient.status")
    track = CURRENT.search(text)
    el = ELAPSED.search(text)
    return (track.group(1) if track else ""), float(el.group(1)) if el else 0.0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]

    print("=== what the graph exposes")
    help_text = telnet(channel, "help")
    for line in help_text.splitlines():
        if "seek" in line.lower():
            print("   ", line.strip())

    crossfade = ""
    env_path = ROOT / "channels" / channel / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("CROSSFADE_SECONDS="):
            crossfade = line.split("=", 1)[1]
    print(f"    crossfade_seconds={crossfade or '(default 5)'}")

    print("\n=== seek on the playlist-fed source")
    track, before = status(channel)
    print(f"    playing {track.split('/')[-1][:44]} at {before:.1f}s")
    print(f"    icecast.seek 30 -> {clean(telnet(channel, 'icecast.seek 30'))!r}")
    time.sleep(3)
    _, after = status(channel)
    moved = after - before - 3
    print(f"    elapsed {before:.1f}s -> {after:.1f}s (drift {moved:+.1f}s)")
    print(f"    {'SEEKED' if abs(moved) > 5 else 'did NOT seek'}")

    print("\n=== seek on a track pushed through the request queue")
    tracks = [
        l.strip() for l in (ROOT / "channels" / channel / "playlist.m3u")
        .read_text().splitlines() if l.strip() and not l.startswith("#")
    ]
    wanted = next((t for t in reversed(tracks) if t != track), tracks[-1])
    print(f"    push {wanted.split('/')[-1][:44]}")
    telnet(channel, f"queue.push {wanted}")
    time.sleep(14)
    now, before = status(channel)
    if now != wanted:
        print("    the push did not land; cannot judge the queue's seekability")
        return 1
    print(f"    on air at {before:.1f}s")
    print(f"    icecast.seek 30 -> {clean(telnet(channel, 'icecast.seek 30'))!r}")
    time.sleep(3)
    _, after = status(channel)
    moved = after - before - 3
    print(f"    elapsed {before:.1f}s -> {after:.1f}s (drift {moved:+.1f}s)")
    print(f"    {'SEEKED' if abs(moved) > 5 else 'did NOT seek'}")

    print("\n=== restoring the playlist")
    telnet(channel, "queue.skip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
