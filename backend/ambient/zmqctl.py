"""Runtime control client for FFmpeg's `zmq` filter.

The validator is security-critical. **Measured** in spike S3: any message that
does not parse into at least two whitespace-separated tokens causes heap
corruption in FFmpeg 6.1.1's `f_zmq.c` and aborts the process with SIGABRT
(exit 134). One malformed string kills a live encoder, so nothing is sent
without passing `build_message` first.

FFmpeg also accepts out-of-range values with `0 Success` and silently ignores
them, so every value is range-checked here. Targeting by bare filter class name
is forbidden: it broadcasts to every instance of that class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SUCCESS = "0 Success"

_TARGET_RE = re.compile(r"^(?P<cls>[A-Za-z][A-Za-z0-9_]*)@(?P<label>[A-Za-z0-9][A-Za-z0-9_]*)$")
_COMMAND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_COLOR_RE = re.compile(r"^(?:#|0x)?[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$|^[A-Za-z]+(?:@0?\.\d+)?$")
_EXPRESSION_CHARS = re.compile(r"^[0-9A-Za-z_.+\-*/%(),^<>=!:?]+$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# FFmpeg's expression grammar, restricted to what a color ramp needs.
_ALLOWED_IDENTIFIERS = frozenset(
    {
        "t", "n", "pos", "w", "h", "PI", "E", "PHI",
        "abs", "acos", "asin", "atan", "atan2", "between", "bitand", "bitor",
        "ceil", "clip", "cos", "cosh", "eq", "exp", "floor", "gauss", "gcd",
        "gt", "gte", "hypot", "if", "ifnot", "isinf", "isnan", "ld", "lerp",
        "log", "lt", "lte", "max", "min", "mod", "not", "pow", "random",
        "root", "round", "sgn", "sin", "sinh", "sqrt", "squish", "st", "tan",
        "tanh", "taylor", "trunc", "while",
    }
)

# Only what the contract lists as commandable, with real ranges. FFmpeg will
# not enforce these.
_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("eq", "contrast"): (-1000.0, 1000.0),
    ("eq", "brightness"): (-1.0, 1.0),
    ("eq", "saturation"): (0.0, 3.0),
    ("eq", "gamma"): (0.1, 10.0),
    ("eq", "gamma_r"): (0.1, 10.0),
    ("eq", "gamma_g"): (0.1, 10.0),
    ("eq", "gamma_b"): (0.1, 10.0),
    ("eq", "gamma_weight"): (0.0, 1.0),
    ("hue", "h"): (-360.0, 360.0),
    ("hue", "H"): (-6.2832, 6.2832),
    ("hue", "s"): (-10.0, 10.0),
    ("hue", "b"): (-10.0, 10.0),
    ("drawbox", "t"): (0.0, 4096.0),
    ("drawbox", "replace"): (0.0, 1.0),
    ("streamselect", "map"): (0.0, 63.0),
    ("astreamselect", "map"): (0.0, 63.0),
    # Timeline switch, not a level: anything but 0 or 1 is a typo.
    ("overlay", "enable"): (0.0, 1.0),
}

_COLOR_PARAMS = frozenset({"color", "c", "colors", "rc", "gc", "bc"})

# `drawbox` re-runs init() on every command and never rolls back: one rejected
# value disables that instance permanently. Expressions are not accepted there.
_NO_EXPRESSION = frozenset({"drawbox", "streamselect", "astreamselect"})

_INTEGER_ONLY = frozenset({
    ("streamselect", "map"), ("astreamselect", "map"), ("overlay", "enable"),
})


class ZmqValidationError(ValueError):
    """The message would be unsafe or meaningless to send."""


class ZmqCommandError(RuntimeError):
    """FFmpeg replied with something other than `0 Success`."""


class ZmqTimeoutError(RuntimeError):
    """The filter did not answer; it only polls when a frame passes through."""


def _single_token(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ZmqValidationError(f"{what} must be a string, got {type(value).__name__}")
    if not value:
        raise ZmqValidationError(f"{what} must not be empty")
    if "\x00" in value:
        raise ZmqValidationError(f"{what} must not contain a NUL byte")
    if len(value.split()) != 1 or value.strip() != value:
        raise ZmqValidationError(f"{what} must be a single whitespace-free token, got {value!r}")
    return value


def validate_target(target: str) -> tuple[str, str]:
    """Only `class@label` is permitted; `all` and bare class names broadcast."""
    token = _single_token(target, "target")
    match = _TARGET_RE.match(token)
    if not match:
        raise ZmqValidationError(
            f"target {token!r} must be of the form class@label — bare class names "
            "broadcast to every instance and `all` targets every filter"
        )
    return match.group("cls"), match.group("label")


def validate_command(command: str) -> str:
    token = _single_token(command, "command")
    if not _COMMAND_RE.match(token):
        raise ZmqValidationError(f"command {token!r} is not a valid parameter name")
    return token


def _validate_expression(filter_class: str, command: str, value: str) -> str:
    if filter_class in _NO_EXPRESSION:
        raise ZmqValidationError(
            f"{filter_class}.{command} does not accept an expression; a rejected value "
            "disables the instance permanently"
        )
    if not _EXPRESSION_CHARS.match(value):
        raise ZmqValidationError(f"value {value!r} contains characters not allowed in an expression")
    unknown = sorted(
        {name for name in _IDENTIFIER.findall(value) if name not in _ALLOWED_IDENTIFIERS}
    )
    if unknown:
        raise ZmqValidationError(f"value {value!r} uses unknown identifier(s): {', '.join(unknown)}")
    return value


def validate_value(filter_class: str, command: str, value: str) -> str:
    """Range-check client-side: an out-of-range value returns `0 Success` and
    is silently ignored."""
    token = _single_token(value, "value")

    if command in _COLOR_PARAMS:
        if not _COLOR_RE.match(token):
            raise ZmqValidationError(f"value {token!r} is not a color")
        return token

    try:
        number = float(token)
    except ValueError:
        return _validate_expression(filter_class, command, token)

    key = (filter_class, command)
    if key in _INTEGER_ONLY and not re.fullmatch(r"[+-]?\d+", token):
        raise ZmqValidationError(f"{filter_class}.{command} takes an integer, got {token!r}")
    bounds = _RANGES.get(key)
    if bounds is not None:
        low, high = bounds
        if not low <= number <= high:
            raise ZmqValidationError(
                f"{filter_class}.{command} value {token} is outside {low}..{high}; "
                "FFmpeg would answer `0 Success` and ignore it"
            )
    return token


def build_message(target: str, command: str, value: str) -> str:
    """Validate and assemble one `TARGET COMMAND ARG` message."""
    filter_class, _label = validate_target(target)
    param = validate_command(command)
    argument = validate_value(filter_class, param, value)
    message = f"{target} {param} {argument}"
    if len(message.split()) != 3:
        raise ZmqValidationError(f"message {message!r} does not parse into exactly three tokens")
    return message


def validate_message(message: str) -> tuple[str, str, str]:
    """Re-validate an assembled message at the send boundary."""
    if not isinstance(message, str):
        raise ZmqValidationError("message must be a string")
    tokens = message.split()
    if len(tokens) != 3 or message.strip() != message or "  " in message:
        raise ZmqValidationError(
            f"message {message!r} must be exactly three non-empty whitespace-free tokens"
        )
    target, command, value = tokens
    build_message(target, command, value)
    return target, command, value


def stream_select_message(target: str, index: int, count: int) -> str:
    """`streamselect@sel map N`, bounds-checked against the hot set."""
    if not isinstance(index, int) or isinstance(index, bool):
        raise ZmqValidationError("stream index must be an int")
    if count <= 0:
        raise ZmqValidationError("hot set is empty")
    if not 0 <= index < count:
        raise ZmqValidationError(f"stream index {index} outside 0..{count - 1}")
    return build_message(target, "map", str(index))


def validate_address(address: str) -> str:
    """The filter's default bind is `tcp://*:5555` — all interfaces, no auth."""
    if not isinstance(address, str) or not address.strip():
        raise ZmqValidationError("address must be a non-empty string")
    address = address.strip()
    if "*" in address or "0.0.0.0" in address:
        raise ZmqValidationError(
            f"address {address!r} is a wildcard bind; the control socket must stay on "
            "loopback or the channel's private network"
        )
    return address


@dataclass
class Reply:
    message: str
    text: str
    round_trip_s: float

    @property
    def ok(self) -> bool:
        return self.text.strip() == SUCCESS


class ZmqControl:
    """One REQ socket against one channel's `zmq` filter instance.

    The filter only polls its socket when a frame passes through, so a round
    trip is bounded below by the graph's frame period (~33 ms at 30 fps) and
    throughput ceilings at roughly one command per frame. Color ramps are sent
    as self-animating expressions rather than command streams for that reason.
    """

    def __init__(self, address: str, timeout_ms: int = 5000) -> None:
        self.address = validate_address(address)
        self.timeout_ms = timeout_ms
        self._ctx: Any = None
        self._sock: Any = None

    def _open(self) -> None:
        try:
            import zmq  # imported lazily so the validator works without pyzmq
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError("pyzmq is required to send runtime commands") from exc
        if self._ctx is None:
            self._ctx = zmq.Context.instance()
        if self._sock is not None:
            self._sock.close(linger=0)
        sock = self._ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(self.address)
        self._sock = sock

    def send(self, target: str, command: str, value: str) -> Reply:
        return self.send_raw(build_message(target, command, value))

    def send_raw(self, message: str) -> Reply:
        import time

        validate_message(message)
        if self._sock is None:
            self._open()
        started = time.monotonic()
        try:
            self._sock.send_string(message)
            text = self._sock.recv_string()
        except Exception as exc:
            self._open()  # a REQ socket that failed mid-exchange cannot be reused
            raise ZmqTimeoutError(f"no reply from {self.address}: {exc}") from exc
        reply = Reply(message=message, text=text, round_trip_s=time.monotonic() - started)
        if not reply.ok:
            raise ZmqCommandError(f"{message!r} -> {text!r}")
        return reply

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close(linger=0)
            self._sock = None

    def __enter__(self) -> "ZmqControl":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
