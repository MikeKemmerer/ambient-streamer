"""Minimal client for FFmpeg's `zmq` / `azmq` filter.

The filter is a plain ZMQ REP socket. A request is the ASCII string
``TARGET COMMAND [ARG]`` and the reply is a status string. The filter only
polls its socket when a frame passes through it, so round-trip time is bounded
below by the graph's frame period.
"""

from __future__ import annotations

import time

import zmq

TIMEOUT = "<TIMEOUT>"


class FFZmq:
    """One REQ socket against one `zmq`/`azmq` filter instance."""

    def __init__(self, addr: str, timeout_ms: int = 5000) -> None:
        self.addr = addr
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._sock: zmq.Socket | None = None
        self._open()

    def _open(self) -> None:
        if self._sock is not None:
            self._sock.close(linger=0)
        sock = self._ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(self.addr)
        self._sock = sock

    def send(self, msg: str, timeout_ms: int | None = None) -> tuple[str, float]:
        """Send one command. Returns (reply, round_trip_seconds)."""
        if timeout_ms is not None and timeout_ms != self.timeout_ms:
            self.timeout_ms = timeout_ms
            self._open()
        started = time.monotonic()
        try:
            self._sock.send_string(msg)
            reply = self._sock.recv_string()
        except zmq.Again:
            self._open()  # a REQ socket that timed out cannot be reused
            return TIMEOUT, time.monotonic() - started
        return reply, time.monotonic() - started

    def wait_ready(self, msg: str, deadline_s: float = 25.0) -> tuple[str, float]:
        """Retry `msg` until the graph answers. Returns (reply, accept_monotonic)."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            reply, _ = self.send(msg, timeout_ms=400)
            if reply != TIMEOUT:
                self.send_timeout_reset()
                return reply, time.monotonic()
        raise TimeoutError(f"no reply from {self.addr} within {deadline_s}s")

    def send_timeout_reset(self, timeout_ms: int = 5000) -> None:
        if self.timeout_ms != timeout_ms:
            self.timeout_ms = timeout_ms
            self._open()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close(linger=0)
            self._sock = None
