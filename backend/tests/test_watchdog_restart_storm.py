"""The watchdog must not fight a restart that is already happening.

Make-before-break runs two composers at once, so the host is briefly
oversubscribed and `speed` dips below `min_speed`. Measured on the live
channel: one operator restart produced 18 YouTube ingest sessions, because each
takeover looked like starvation and triggered the next restart.

The supervisor's per-channel lock is the signal - it is held for the whole of a
start, stop or restart, including the takeover wait.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ambient.config import load_workspace
from ambient.models import ChannelState, Health
from ambient.watchdog import SETTLE_SECONDS, ProgressSample, Verdict, Watchdog


class FakeSupervisor:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self.restarts: list[str] = []
        self.starts: list[str] = []

    def lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def containers(self, name: str):  # pragma: no cover - act() never needs it
        raise AssertionError("act() must not need containers")

    async def read_run_file(self, name: str, filename: str, *, tail_bytes: int = 0) -> str:
        return ""

    async def restart(self, name: str) -> dict[str, object]:
        self.restarts.append(name)
        return {}

    async def start(self, name: str):
        self.starts.append(name)


def starving() -> Verdict:
    return Verdict(
        ChannelState.DEGRADED,
        Health.STARVING,
        fault="speed_below_minimum",
        detail="speed 0.885x below 0.97x for 16.4s; YouTube is starving",
        sample=ProgressSample(speed=0.885, out_time_us=1_000_000),
    )


def make(repo: Path, supervisor: FakeSupervisor) -> Watchdog:
    return Watchdog(workspace=load_workspace(repo), supervisor=supervisor)


def test_a_starving_channel_is_restarted_when_nothing_else_is_running(repo: Path) -> None:
    supervisor = FakeSupervisor()
    watchdog = make(repo, supervisor)

    acted = asyncio.run(watchdog.act("lofi", starving(), 1000.0))

    assert acted is True
    assert supervisor.restarts == ["lofi"]


def test_no_restart_while_the_supervisor_holds_the_channel(repo: Path) -> None:
    supervisor = FakeSupervisor()
    watchdog = make(repo, supervisor)

    async def scenario() -> bool:
        async with supervisor.lock("lofi"):
            return await watchdog.act("lofi", starving(), 1000.0)

    assert asyncio.run(scenario()) is False
    assert supervisor.restarts == []


def test_the_channel_gets_a_settle_window_after_the_operation_clears(repo: Path) -> None:
    """Otherwise the very next poll judges a composer that is still spinning up."""
    supervisor = FakeSupervisor()
    watchdog = make(repo, supervisor)

    async def scenario() -> tuple[float, bool, bool]:
        async with supervisor.lock("lofi"):
            await watchdog.act("lofi", starving(), 1000.0)
        settle = watchdog._watch("lofi").settle_until
        # Still inside the settle window: a fault must not restart it again.
        inside = await watchdog.act("lofi", starving(), 1000.0 + SETTLE_SECONDS - 1)
        # Past it, the watchdog is back on duty.
        outside = await watchdog.act("lofi", starving(), 1000.0 + SETTLE_SECONDS + 1)
        return settle, inside, outside

    settle, inside, outside = asyncio.run(scenario())

    assert settle == pytest.approx(1000.0 + SETTLE_SECONDS)
    assert inside is False
    assert outside is True
    assert supervisor.restarts == ["lofi"]


def test_a_supervisor_without_a_lock_is_tolerated(repo: Path) -> None:
    """The protocol is structural; a test double need not implement every member."""

    class NoLock(FakeSupervisor):
        lock = None  # type: ignore[assignment]

    supervisor = NoLock()
    watchdog = make(repo, supervisor)

    assert asyncio.run(watchdog.act("lofi", starving(), 1000.0)) is True
