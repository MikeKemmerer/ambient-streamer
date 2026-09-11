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
from collections.abc import Iterable

# `icecast.skip` is the one that moves the audio. `playlist.skip` answers OK and
# advances the playlist cursor without changing what is playing - measured on a
# live channel, so it is deliberately not offered here.
SKIP = "icecast.skip"
STATUS = "ambient.status"
CURRENT = "ambient.current"
NEXT = "ambient.next"

# Takes an argument, so it cannot go through the plain whitelist - see `push`.
PUSH = "queue.push"
SOUNDBOARD_PUSH = "soundboard.push"
SOUNDBOARD_STOP = "soundboard.flush_and_skip"

_ALLOWED = frozenset({SKIP, STATUS, CURRENT, NEXT, SOUNDBOARD_STOP})

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
    return _exchange(host, validate_command(command), port=port, timeout=timeout)


def _exchange(host: str, line: str, *, port: int, timeout: float) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"{line}\nquit\n".encode())
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
        stripped
        for stripped in (
            raw.strip() for raw in b"".join(chunks).decode("utf-8", "replace").splitlines()
        )
        if stripped and stripped not in {"END", "Bye!"}
    ]
    if not lines:
        raise LiquidsoapError(f"{host}:{port} answered nothing to {line!r}")
    return "\n".join(lines)


def liquidsoap_host(channel: str) -> str:
    """Matches the container name the per-channel Compose file pins."""
    return f"{channel}-liquidsoap"


def validate_uri(uri: str, allowed: Iterable[str]) -> str:
    """Only a track this channel already resolved may be put on air.

    `queue.push` resolves whatever it is handed, so an unchecked value here is
    an arbitrary file read on the Liquidsoap container and an outbound request
    for any `http://` URI. The membership test is the whole defence; the
    newline check only stops a second command riding along on the same line.
    """
    token = uri.strip()
    if not token:
        raise LiquidsoapError("no track given")
    if any(ch in token for ch in "\r\n"):
        raise LiquidsoapError("track contains a line break")
    if token not in set(allowed):
        raise LiquidsoapError(f"{uri!r} is not a track on this channel")
    return token


def push(
    host: str,
    uri: str,
    *,
    allowed: Iterable[str],
    port: int = TELNET_PORT,
    timeout: float = 5.0,
) -> str:
    """Put one specific track on air, interrupting whatever is playing."""
    return _exchange(host, f"{PUSH} {validate_uri(uri, allowed)}", port=port, timeout=timeout)


def push_soundboard(
    host: str,
    uri: str,
    *,
    allowed: Iterable[str],
    port: int = TELNET_PORT,
    timeout: float = 5.0,
) -> str:
    """Queue one validated effect on the dedicated overlay source."""
    try:
        token = validate_uri(uri, allowed)
    except LiquidsoapError as exc:
        raise LiquidsoapError(str(exc).replace("track", "soundboard clip")) from exc
    return _exchange(host, f"{SOUNDBOARD_PUSH} {token}", port=port, timeout=timeout)
