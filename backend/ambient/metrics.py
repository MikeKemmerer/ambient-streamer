"""Optional Prometheus exposition.

Hand-written text format rather than a client library: the whole surface is a
handful of gauges and one counter, and the backend runs in a container where
every dependency is weight.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .models import ChannelState, Health
from .watchdog import Verdict, Watchdog

PREFIX = "ambient"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(name: str, labels: Mapping[str, str], value: float) -> str:
    tags = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return f"{PREFIX}_{name}{{{tags}}} {value:g}"


def _metric(name: str, kind: str, help_text: str, samples: Iterable[str]) -> list[str]:
    rows = list(samples)
    if not rows:
        return []
    return [f"# HELP {PREFIX}_{name} {help_text}", f"# TYPE {PREFIX}_{name} {kind}", *rows]


def render(watchdog: Watchdog, *, cpu: Mapping[str, float] | None = None) -> str:
    """Prometheus text for every channel the watchdog currently knows."""
    verdicts: dict[str, Verdict] = dict(watchdog.latest)
    cpu = cpu or {}
    lines: list[str] = []

    lines += _metric(
        "channel_up",
        "gauge",
        "1 when the channel's compositor is running.",
        (
            _line("channel_up", {"channel": name}, float(v.state is ChannelState.RUNNING))
            for name, v in verdicts.items()
        ),
    )
    lines += _metric(
        "channel_healthy",
        "gauge",
        "1 when the watchdog's last verdict was healthy.",
        (
            _line("channel_healthy", {"channel": name}, float(v.health is Health.HEALTHY))
            for name, v in verdicts.items()
        ),
    )
    lines += _metric(
        "channel_state",
        "gauge",
        "1 for the channel's current state, 0 for the others.",
        (
            _line("channel_state", {"channel": name, "state": state.value}, float(v.state is state))
            for name, v in verdicts.items()
            for state in ChannelState
        ),
    )
    lines += _metric(
        "channel_speed",
        "gauge",
        "FFmpeg encode speed. Below 0.97 for long enough is a starving stream.",
        (
            _line("channel_speed", {"channel": name}, v.sample.speed)
            for name, v in verdicts.items()
            if v.sample is not None
        ),
    )
    lines += _metric(
        "channel_fps",
        "gauge",
        "FFmpeg output frame rate.",
        (
            _line("channel_fps", {"channel": name}, v.sample.fps)
            for name, v in verdicts.items()
            if v.sample is not None
        ),
    )
    lines += _metric(
        "channel_out_time_seconds",
        "gauge",
        "Media time published. Not advancing against wallclock is a stall.",
        (
            _line("channel_out_time_seconds", {"channel": name}, v.sample.out_time_seconds)
            for name, v in verdicts.items()
            if v.sample is not None
        ),
    )
    lines += _metric(
        "channel_cpu_cores",
        "gauge",
        "Cores used by the channel's containers.",
        (_line("channel_cpu_cores", {"container": key}, value) for key, value in cpu.items()),
    )
    lines += _metric(
        "watchdog_restarts_total",
        "counter",
        "Restarts the watchdog has performed for this channel.",
        (
            _line("watchdog_restarts_total", {"channel": name}, float(watch.restarts))
            for name, watch in watchdog.channels.items()
        ),
    )
    lines += _metric(
        "sse_subscribers",
        "gauge",
        "Connected SSE clients.",
        [f"{PREFIX}_sse_subscribers {watchdog.events.subscriber_count if watchdog.events else 0}"],
    )
    lines += _metric(
        "sse_dropped_subscribers_total",
        "counter",
        "SSE clients dropped for falling behind.",
        [
            f"{PREFIX}_sse_dropped_subscribers_total "
            f"{watchdog.events.dropped_subscribers if watchdog.events else 0}"
        ],
    )
    return "\n".join(lines) + "\n"


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
