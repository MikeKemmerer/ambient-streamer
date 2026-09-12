#!/usr/bin/env python3
"""Keep a compositor layer alive while visualization writers come and go."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path

MAX_WIDTH = 1280
MAX_HEIGHT = 720
MAX_FPS = 30.0


def black_frame(width: int, height: int) -> bytes:
    pixels = width * height
    return bytes([16]) * pixels + bytes([128]) * (pixels // 2)


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


def validate_geometry(width: int, height: int, fps: float) -> None:
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("width and height must be positive even integers")
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        raise ValueError(f"geometry exceeds {MAX_WIDTH}x{MAX_HEIGHT}")
    if fps <= 0 or fps > MAX_FPS:
        raise ValueError(f"fps must be greater than 0 and at most {MAX_FPS:g}")


def validate_state_path(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"{label} parent must be a real directory")
    if path.is_symlink():
        raise ValueError(f"{label} path must not be a symlink")


def write_status(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_status(path: Path, payload: dict[str, object]) -> None:
    try:
        write_status(path, payload)
    except OSError as error:
        log("status-write-failed", error=type(error).__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float, required=True)
    parser.add_argument("--stale-seconds", type=float, default=0.35)
    parser.add_argument("--status", required=True)
    args = parser.parse_args()

    try:
        validate_geometry(args.width, args.height, args.fps)
        if args.stale_seconds <= 0:
            raise ValueError("stale-seconds must be greater than 0")
        socket_path = Path(args.input)
        status_path = Path(args.status)
        validate_state_path(socket_path, "input")
        validate_state_path(status_path, "status")
    except ValueError as error:
        parser.error(str(error))

    frame_size = args.width * args.height * 3 // 2
    fallback = black_frame(args.width, args.height)
    output = sys.stdout.buffer
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)
    server.listen(1)
    server.settimeout(0.1)
    reader: socket.socket | None = None
    latest: bytes | None = None
    latest_at = 0.0
    state_lock = threading.Lock()
    deadline = time.monotonic()
    emitted = received = fallbacks = reconnects = 0
    last_fallback = True
    last_status_at = 0.0
    stopping = False

    def status(ready: bool, fallback_active: bool) -> dict[str, object]:
        with state_lock:
            current_at = latest_at
        age = None if current_at == 0 else max(0.0, time.monotonic() - current_at)
        return {
            "ready": ready,
            "fallback": fallback_active,
            "emitted": emitted,
            "received": received,
            "fallbacks": fallbacks,
            "reconnects": reconnects,
            "last_frame_age_seconds": None if age is None else round(age, 3),
            "updated_at": int(time.time()),
        }

    def stop(_signum: int, _frame: object) -> None:
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
                    log("writer-connected", reconnects=reconnects)
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
                log("writer-disconnected")
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
    publish_status(status_path, status(ready=True, fallback_active=True))
    log(
        "ready",
        socket=str(socket_path),
        width=args.width,
        height=args.height,
        fps=args.fps,
    )

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
                publish_status(status_path, status(True, use_fallback))
                last_status_at = now

            output.write(fallback if use_fallback else current)
            output.flush()
            emitted += 1
            if now - last_status_at >= 1.0:
                publish_status(status_path, status(True, use_fallback))
                last_status_at = now
            deadline += 1.0 / args.fps
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()
    except BrokenPipeError:
        pass
    finally:
        stopping = True
        if reader is not None:
            reader.close()
        server.close()
        receiver.join(timeout=0.5)
        socket_path.unlink(missing_ok=True)
        publish_status(status_path, status(ready=False, fallback_active=True))
        log("stopped", emitted=emitted, received=received)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())