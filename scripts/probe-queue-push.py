#!/usr/bin/env python3
"""Push one request and show exactly what Liquidsoap did with it.

Usage: probe-queue-push.py <channel> [track-index-from-end]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "ambient-streamer"


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


def main() -> int:
    channel = sys.argv[1]
    back = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    tracks = [
        l.strip() for l in
        (ROOT / "channels" / channel / "playlist.m3u").read_text().splitlines()
        if l.strip() and not l.startswith("#")
    ]
    wanted = tracks[-back]

    print(f"before: {telnet(channel, 'ambient.status').splitlines()[0][:110]}")
    print(f"push:   {wanted}")
    print(f"reply:  {telnet(channel, f'queue.push {wanted}').splitlines()[0]!r}")

    elapsed = 0
    for step in (2, 4, 6, 8):
        time.sleep(step)
        elapsed += step
        status = telnet(channel, "ambient.status").splitlines()[0]
        queued = telnet(channel, "queue.queue").splitlines()[0]
        print(f"  +{elapsed:>2}s queue={queued!r:<12} {status[:100]}")

    print("\n--- liquidsoap log since the push")
    log = subprocess.run(
        ["docker", "logs", "--since", "40s", f"{channel}-liquidsoap"],
        capture_output=True, text=True)
    for line in (log.stdout + log.stderr).splitlines():
        if "server:" not in line:
            print("   ", line[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
