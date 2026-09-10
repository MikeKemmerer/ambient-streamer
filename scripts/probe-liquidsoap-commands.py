#!/usr/bin/env python3
"""What can Liquidsoap actually be told to do at runtime?

Skip and seek are only worth designing around if the running source supports
them. `source.seek` in particular depends on the source being seekable, and a
crossfaded playlist may not be. This asks the live telnet rather than the docs.

Usage: probe-liquidsoap-commands.py <channel>
"""
from __future__ import annotations

import socket
import subprocess
import sys


def container(channel: str) -> str:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={channel}-liquidsoap", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    return out[0] if out else ""


def telnet(channel: str, commands: list[str]) -> dict[str, str]:
    """Run from the composer, which has python and reaches Liquidsoap by name.

    The Liquidsoap image has no python, and the telnet port is deliberately
    unpublished, so this is the same route nowstate.py already uses.
    """
    script = "\n".join(commands + ["quit", ""])
    result = subprocess.run(
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
        capture_output=True, text=True, timeout=90)
    return {"out": result.stdout, "err": result.stderr[:300]}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    channel = sys.argv[1]
    if not container(channel):
        print(f"{channel}-liquidsoap is not running")
        return 1

    print("=== every command the running instance exposes ===")
    print(telnet(channel, ["help"])["out"])

    print("=== what the playlist and the output source offer ===")
    for probe in ["help playlist", "help radio", "playlist.next", "ambient.status"]:
        answer = telnet(channel, [probe])
        print(f"--- {probe}\n{answer['out'].strip()[:600]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
