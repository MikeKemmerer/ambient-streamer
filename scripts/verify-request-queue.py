#!/usr/bin/env python3
"""Does pushing a request actually put THAT track on air?

`playlist` has no "play this one" verb - measured: `playlist.skip` answers OK
and leaves the audio where it was - so a request queue in front of it is the
only way to honour a choice. This checks the queue is really in the path and
that a push interrupts rather than waiting for the current track to end.

Usage: verify-request-queue.py <channel>
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"
CURRENT = re.compile(r"current='([^']*)'")


def telnet(channel: str, *commands: str) -> str:
    script = "\n".join([*commands, "quit", ""])
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "ambient", "--entrypoint", "python3",
         "ambient-composer:dev", "-c", f'''
import socket, sys
s = socket.create_connection(("{channel}-liquidsoap", 1234), timeout=10)
s.sendall({script!r}.encode())
buf = b""
s.settimeout(3)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
except Exception:
    pass
sys.stdout.write(buf.decode("utf-8", "replace"))
'''],
        capture_output=True, text=True, timeout=90).stdout


def current(channel: str) -> str:
    found = CURRENT.search(telnet(channel, "ambient.status"))
    return found.group(1) if found else ""


def playlist(channel: str) -> list[str]:
    path = ROOT / "channels" / channel / "playlist.m3u"
    return [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    tracks = playlist(channel)
    if len(tracks) < 3:
        print("needs at least 3 tracks")
        return 2

    playing = current(channel)
    print(f"playing {playing.split('/')[-1][:50]}")

    # Something well away from what is on air, so "it was going to play next
    # anyway" cannot explain the result.
    wanted = next((t for t in reversed(tracks) if t != playing), tracks[-1])
    print(f"asking for {wanted.split('/')[-1][:50]}\n")

    reply = [l for l in telnet(channel, f"queue.push {wanted}").splitlines()
             if l.strip() and l not in {"END", "Bye!"}]
    check("push accepted", bool(reply), " ".join(reply)[:40])

    # track_sensitive=false, so it should interrupt rather than wait.
    time.sleep(14)
    now = current(channel)
    check("the requested track is on air", now == wanted,
          now.split("/")[-1][:50])
    check("it interrupted rather than queued behind", now != playing,
          "track_sensitive=false")

    print("\n  letting it fall back to the playlist")
    telnet(channel, "queue.skip")
    time.sleep(14)
    after = current(channel)
    check("fell back to the playlist", after != wanted and after in tracks,
          after.split("/")[-1][:50])

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("a pushed request plays, then the playlist resumes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
