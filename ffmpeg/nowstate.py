#!/usr/bin/env python3
"""Writer for ``/run/ambient/<channel>/now.json``.

Three processes hold the facts this file reports and none of them can write it
alone: Liquidsoap owns the current and next track and answers on its telnet
socket, the slideshow producer owns the current slide, and the compositor
entrypoint owns the active plugin. This module merges them on a daemon thread
inside the producer — the only long-lived Python process in the compositor
container, and the one that already holds the slide.

It is telemetry, never the signal path. Every failure is logged and swallowed;
nothing here may stop frames being produced.

See docs/contracts/on-disk.md.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

LOG = sys.stderr

# The container mounts from on-disk.md, mapped back to repo-relative paths so
# the operator sees the same strings that appear in config.yaml.
COMMON_MOUNT = "/media/common/"
CHANNEL_MOUNT = "/media/channel/"


def log(event: str, **fields: object) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"nowstate event={event} {parts}", file=LOG, flush=True)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_relative(path: str, channel: str) -> str:
    if path.startswith(COMMON_MOUNT):
        return "common/" + path[len(COMMON_MOUNT):]
    if path.startswith(CHANNEL_MOUNT):
        return f"channels/{channel}/" + path[len(CHANNEL_MOUNT):]
    return path


def atomic_write_json(path: str, payload: dict[str, object]) -> None:
    """Temp file in the *same* directory, then rename.

    A temp file under /tmp followed by a move is a cross-device copy, not an
    atomic rename, and the backend reads this file concurrently.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".now.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out, indent=2, sort_keys=True)
            out.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Liquidsoap telnet
# --------------------------------------------------------------------------

def _read_block(sock: socket.socket, buf: bytearray) -> str:
    """Liquidsoap terminates every telnet response with a bare ``END`` line."""
    while True:
        blob = bytes(buf).replace(b"\r\n", b"\n")
        marker = blob.find(b"END\n")
        if marker == 0 or (marker > 0 and blob[marker - 1] == 0x0A):
            payload = blob[: marker - 1 if marker else 0]
            buf[:] = blob[marker + 4:]
            return payload.decode("utf-8", "replace")
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("liquidsoap closed the connection")
        buf.extend(chunk)


def ask(host: str, port: int, commands: tuple[str, ...], timeout: float) -> list[str]:
    """One short-lived connection per poll: a held socket survives no restart."""
    replies: list[str] = []
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        buf = bytearray()
        for command in commands:
            sock.sendall(command.encode("ascii") + b"\n")
            replies.append(_read_block(sock, buf))
        try:
            sock.sendall(b"quit\n")
        except OSError:
            pass
    return replies


def last_value(block: str) -> str:
    """The value is the last non-empty line, so any banner is discarded."""
    for line in reversed(block.splitlines()):
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NowSettings:
    path: str
    channel: str
    active_plugin: str
    started_at: str
    liq_host: str
    liq_port: int
    interval: float
    timeout: float


class NowWriter(threading.Thread):
    def __init__(self, cfg: NowSettings) -> None:
        super().__init__(daemon=True, name="nowstate")
        self.cfg = cfg
        self.lock = threading.Lock()
        self.slide = ""
        self.track = ""
        self.next_track = ""

    def set_slide(self, path: str) -> None:
        with self.lock:
            self.slide = path

    def poll_tracks(self) -> None:
        try:
            current, upcoming = ask(
                self.cfg.liq_host, self.cfg.liq_port,
                ("ambient.current", "ambient.next"), self.cfg.timeout,
            )
        except (OSError, ValueError) as exc:
            # Liquidsoap may be restarting; Icecast's fallback keeps audio up,
            # so the last known pair is closer to the truth than a blank.
            log("liquidsoap_unreachable", host=self.cfg.liq_host,
                port=self.cfg.liq_port, error=type(exc).__name__)
            return
        self.track = last_value(current)
        self.next_track = last_value(upcoming)

    def rel(self, value: str) -> Optional[str]:
        return repo_relative(value, self.cfg.channel) or None

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            slide = self.slide
        return {
            "current_track": self.rel(self.track),
            "next_track": self.rel(self.next_track),
            "current_slide": self.rel(slide),
            "active_plugin": self.cfg.active_plugin or None,
            "started_at": self.cfg.started_at,
        }

    def run(self) -> None:
        log("start", path=self.cfg.path, liquidsoap=
            f"{self.cfg.liq_host}:{self.cfg.liq_port}",
            interval=self.cfg.interval, active_plugin=self.cfg.active_plugin)
        while True:
            try:
                self.poll_tracks()
                atomic_write_json(self.cfg.path, self.snapshot())
            except Exception as exc:  # telemetry must never reach the stream
                log("write_failed", path=self.cfg.path, error=type(exc).__name__)
            time.sleep(self.cfg.interval)


def settings_from_env() -> Optional[NowSettings]:
    channel = os.environ.get("CHANNEL_NAME", "").strip()
    if not channel:
        return None
    run_dir = os.environ.get("RUN_DIR", "") or f"/run/ambient/{channel}"
    try:
        port = int(os.environ.get("LIQ_TELNET_PORT", "") or 1234)
        interval = float(os.environ.get("NOW_INTERVAL_SECONDS", "") or 2.0)
    except ValueError:
        log("bad_env", fallback="port=1234 interval=2.0")
        port, interval = 1234, 2.0
    return NowSettings(
        path=os.environ.get("NOW_FILE", "") or os.path.join(run_dir, "now.json"),
        channel=channel,
        active_plugin=os.environ.get("ACTIVE_PLUGIN", "").strip(),
        started_at=os.environ.get("COMPOSER_STARTED_AT", "").strip() or utc_now(),
        liq_host=os.environ.get("LIQ_TELNET_HOST", "") or f"{channel}-liquidsoap",
        liq_port=port,
        interval=max(0.5, interval),
        timeout=2.0,
    )


def start_writer() -> Optional[NowWriter]:
    """Started only when the compositor configured it; None otherwise."""
    cfg = settings_from_env()
    if cfg is None:
        return None
    writer = NowWriter(cfg)
    writer.start()
    return writer
