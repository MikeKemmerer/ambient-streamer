#!/usr/bin/env python3
"""Do skip and seek actually work on the source this channel runs?

`icecast.seek` is listed by the running instance, but listing is not working:
seeking needs a seekable source, and this one is a playlist behind `crossfade`
and `mksafe`. Both wrappers can swallow a seek. This drives the real telnet and
reads `ambient.status` either side.

Usage: probe-track-control.py <channel>
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

ELAPSED = re.compile(r"elapsed=([0-9.]+)")
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


def status(channel: str) -> tuple[str, float]:
    text = telnet(channel, "ambient.status")
    track = CURRENT.search(text)
    elapsed = ELAPSED.search(text)
    return (track.group(1).split("/")[-1] if track else "?",
            float(elapsed.group(1)) if elapsed else -1.0)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    failures: list[str] = []

    def check(label: str, ok: bool, note: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<40} {note}")
        if not ok:
            failures.append(label)

    track, elapsed = status(channel)
    print(f"playing {track} at {elapsed:.1f}s\n")

    print("seek +30s")
    reply = telnet(channel, "icecast.seek 30").strip().splitlines()
    print(f"  reply: {[l for l in reply if l and l != 'Bye!'][:2]}")
    time.sleep(3)
    after_track, after_elapsed = status(channel)
    # elapsed comes from our own ref, set on track change, so a seek does not
    # move it. The reply is what says whether the source accepted the seek.
    seeked = any("30" in line or line.strip().isdigit() for line in reply)
    check("seek was accepted", seeked, " ".join(reply)[:60])
    check("seek did not change track", after_track == track, after_track)

    print("\nskip")
    before = after_track
    telnet(channel, "icecast.skip")
    time.sleep(5)
    now, now_elapsed = status(channel)
    check("skip moved to another track", now != before, f"{before} -> {now}")
    check("elapsed restarted", now_elapsed < 15, f"{now_elapsed:.1f}s")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("skip and seek both work on this source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
