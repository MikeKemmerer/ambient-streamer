#!/usr/bin/env python3
"""Keep a compositor layer alive while visualization writers come and go."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path


def black_frame(width: int, height: int) -> bytes:
    pixels = width * height
    return bytes([16]) * pixels + bytes([128]) * (pixels // 2)


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float, required=True)
    parser.add_argument("--stale-seconds", type=float, default=0.35)
    parser.add_argument("--status", required=True)
    args = parser.parse_args()

    frame_size = args.width * args.height * 3 // 2
    fallback = black_frame(args.width, args.height)
    output = sys.stdout.buffer
    socket_path = Path(args.input)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    server.settimeout(0.1)
    reader: socket.socket | None = None
    latest: bytes | None = None
    latest_at = 0.0
    state_lock = threading.Lock()
    deadline = time.monotonic()
    emitted = received = fallbacks = reconnects = 0
    last_fallback = True
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def receive() -> None:
        nonlocal reader, latest, latest_at, received, reconnects
        partial = bytearray()
        while not stopping:
            if reader is None:
                try:
                    reader, _address = server.accept()
                    reader.settimeout(0.1)
                    reconnects += 1
                except TimeoutError:
                    continue
                except OSError:
                    if stopping:
                        return
                    continue
            try:
                chunk = reader.recv(frame_size * 4)
            except TimeoutError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                reader.close()
                reader = None
                partial.clear()
                continue
            partial.extend(chunk)
            while len(partial) >= frame_size:
                complete = bytes(partial[:frame_size])
                del partial[:frame_size]
                with state_lock:
                    latest = complete
                    latest_at = time.monotonic()
                    received += 1

    receiver = threading.Thread(target=receive, daemon=True, name="viz-receiver")
    receiver.start()

    try:
        while not stopping:
            now = time.monotonic()
            with state_lock:
                current, current_at = latest, latest_at
            use_fallback = current is None or now - current_at > args.stale_seconds
            if use_fallback:
                fallbacks += 1
            if use_fallback != last_fallback:
                log("fallback" if use_fallback else "visualization", frame=emitted)
                last_fallback = use_fallback

            output.write(fallback if use_fallback else current)
            output.flush()
            emitted += 1
            deadline += 1.0 / args.fps
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()
    except BrokenPipeError:
        pass
    finally:
        if reader is not None:
            reader.close()
        server.close()
        socket_path.unlink(missing_ok=True)
        Path(args.status).write_text(
            json.dumps(
                {
                    "emitted": emitted,
                    "received": received,
                    "fallbacks": fallbacks,
                    "reconnects": reconnects,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())