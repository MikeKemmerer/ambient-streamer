"""Runtime control client for Liquidsoap's telnet server.

Separate from `zmqctl` because the two control planes answer different
questions: zmq changes what the compositor draws, this changes what the audio
source is playing. Liquidsoap owns audio in its own process, so a track change
here costs the video nothing at all — the compositor is a consumer of a live
Icecast mount and never learns a track changed.

The telnet socket is a full command interface, including `shutdown`. It is
reachable only on the compose network and is never published, and this client
refuses to send anything outside a whitelist so a bug here cannot stop a
channel.
"""

from __future__ import annotations

import socket

# `icecast.skip` is the one that moves the audio. `playlist.skip` answers OK and
# advances the playlist cursor without changing what is playing - measured on a
# live channel, so it is deliberately not offered here.
SKIP = "icecast.skip"
STATUS = "ambient.status"
CURRENT = "ambient.current"
NEXT = "ambient.next"

_ALLOWED = frozenset({SKIP, STATUS, CURRENT, NEXT})

TELNET_PORT = 1234


class LiquidsoapError(RuntimeError):
    """The command could not be delivered, or the answer was not understood."""


def validate_command(command: str) -> str:
    """A whitelist, not an escape: `shutdown` is one word away from an outage."""
    token = command.strip()
    if token not in _ALLOWED:
        raise LiquidsoapError(
            f"{command!r} is not an allowed Liquidsoap command; "
            f"expected one of {', '.join(sorted(_ALLOWED))}"
        )
    return token


def send(host: str, command: str, *, port: int = TELNET_PORT, timeout: float = 5.0) -> str:
    """Send one command and return the reply with the protocol lines removed."""
    token = validate_command(command)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"{token}\nquit\n".encode())
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        raise LiquidsoapError(f"{host}:{port} did not answer: {exc}") from exc

    lines = [
        line.strip()
        for line in b"".join(chunks).decode("utf-8", "replace").splitlines()
        if line.strip() and line.strip() not in {"END", "Bye!"}
    ]
    if not lines:
        raise LiquidsoapError(f"{host}:{port} answered nothing to {token!r}")
    return "\n".join(lines)


def liquidsoap_host(channel: str) -> str:
    """Matches the container name the per-channel Compose file pins."""
    return f"{channel}-liquidsoap"
