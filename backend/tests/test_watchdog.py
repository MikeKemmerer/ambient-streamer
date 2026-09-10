"""Watchdog: the two measured failure modes, and the backoff that follows.

Neither mode is a liveness problem, so every test here fakes a progress file
rather than needing a real stream.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ambient.config import load_workspace
from ambient.events import EventHub
from ambient.models import ChannelState, Health
from ambient.supervisor import ChannelContainers, ContainerInfo
from ambient.watchdog import ChannelHealth, Watchdog, parse_progress

COMPOSER = ContainerInfo(name="lofi-composer", exists=True, running=True, status="running")
EXITED_OK = ContainerInfo(
    name="lofi-composer", exists=True, running=False, status="exited", exit_code=0
)
ABSENT = ContainerInfo(name="lofi-composer")
LIQUIDSOAP = ContainerInfo(name="lofi-liquidsoap", exists=True, running=True, status="running")


def progress_block(out_time_us: int, speed: float, *, frame: int = 0) -> str:
    return (
        f"frame={frame}\n"
        "fps=30.0\n"
        "bitrate=3000.0kbits/s\n"
        "total_size=1024\n"
        f"out_time_us={out_time_us}\n"
        f"out_time={out_time_us / 1_000_000:.6f}\n"
        # Measured: both stay at 0 through the burst-pacing failure, so they
        # are parsed but never used as a health signal.
        "dup_frames=0\n"
        "drop_frames=0\n"
        f"speed={speed:.3f}x\n"
        "progress=continue\n"
    )


@dataclass
class FakeRuntime:
    composer: ContainerInfo = COMPOSER
    liquidsoap: ContainerInfo = LIQUIDSOAP
    progress: str = ""
    restarts: list[str] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)

    async def containers(self, name: str) -> ChannelContainers:
        return ChannelContainers(composer=self.composer, liquidsoap=self.liquidsoap)

    async def read_run_file(self, name: str, filename: str, *, tail_bytes: int = 8192) -> str:
        return self.progress

    async def restart(self, name: str) -> dict[str, object]:
        self.restarts.append(name)
        return {"took_over": True}

    async def start(self, name: str):
        self.starts.append(name)
        return None


def make_watchdog(repo: Path, runtime: FakeRuntime, events: EventHub | None = None) -> Watchdog:
    return Watchdog(workspace=load_workspace(repo), supervisor=runtime, events=events)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_takes_the_last_complete_block() -> None:
    text = progress_block(1_000_000, 1.0) + progress_block(2_000_000, 0.44)
    sample = parse_progress(text)
    assert sample is not None
    assert sample.out_time_us == 2_000_000
    assert sample.speed == pytest.approx(0.44)
    assert sample.bitrate_kbps == pytest.approx(3000.0)


def test_a_truncated_leading_block_is_discarded() -> None:
    text = "out_time_us=999\nspeed=0.5x\n" + progress_block(2_000_000, 1.0)
    sample = parse_progress(text)
    assert sample is not None and sample.out_time_us == 2_000_000


def test_no_progress_output_parses_to_nothing() -> None:
    assert parse_progress("") is None
    assert parse_progress("garbage\n") is None


# --------------------------------------------------------------------------
# Failure mode 1: a stalled producer kills nothing
# --------------------------------------------------------------------------


def test_a_frozen_out_time_is_a_stall_even_though_ffmpeg_is_alive() -> None:
    health = ChannelHealth("lofi", min_speed=0.97, stall_seconds=15.0)
    frozen = parse_progress(progress_block(10_000_000, 0.44))

    assert health.observe(0.0, frozen).fault is None  # first sighting
    assert health.observe(30.0, frozen).fault is None  # still inside the startup grace
    verdict = health.observe(80.0, frozen)

    assert verdict.fault == "out_time_stalled"
    assert verdict.health is Health.STALLED
    assert verdict.state is ChannelState.DEGRADED


def test_sustained_low_speed_is_starving_even_while_out_time_creeps() -> None:
    health = ChannelHealth("lofi", min_speed=0.97, stall_seconds=15.0)
    now = 0.0
    verdict = health.observe(now, parse_progress(progress_block(0, 1.0)))
    # Past the startup grace, then a long run at 0.44x with out_time still moving.
    for step in range(1, 12):
        now = 50.0 + step * 5.0
        micros = int(step * 2_000_000)
        verdict = health.observe(now, parse_progress(progress_block(micros, 0.44)))

    assert verdict.fault == "speed_below_minimum"
    assert verdict.health is Health.STARVING


def test_healthy_progress_stays_healthy_with_drop_and_dup_at_zero() -> None:
    health = ChannelHealth("lofi", min_speed=0.97, stall_seconds=15.0)
    verdict = None
    for step in range(20):
        now = float(step) * 5.0
        verdict = health.observe(now, parse_progress(progress_block(step * 5_000_000, 1.0)))
    assert verdict is not None
    assert verdict.fault is None
    assert verdict.health is Health.HEALTHY
    assert verdict.state is ChannelState.RUNNING


def test_startup_is_not_called_a_stall() -> None:
    health = ChannelHealth("lofi")
    sample = parse_progress(progress_block(0, 0.0))
    assert health.observe(0.0, sample).state is ChannelState.STARTING
    assert health.observe(20.0, sample).fault is None


# --------------------------------------------------------------------------
# Failure mode 2: a dead producer makes FFmpeg exit rc=0
# --------------------------------------------------------------------------


def test_a_zero_exit_code_is_still_a_fault() -> None:
    verdict = ChannelHealth("lofi").observe_exit(0)
    assert verdict.fault == "composer_exit"
    assert verdict.state is ChannelState.FAILED
    assert verdict.health is Health.DISCONNECTED
    assert "exit 0" in verdict.detail


def test_a_never_started_channel_is_stopped_not_failed() -> None:
    verdict = ChannelHealth("lofi").observe_stopped()
    assert verdict.state is ChannelState.STOPPED
    assert verdict.fault is None


def test_watchdog_restarts_a_composer_that_exited_zero(repo: Path) -> None:
    runtime = FakeRuntime(composer=EXITED_OK)
    watchdog = make_watchdog(repo, runtime)

    verdicts = asyncio.run(watchdog.poll_once(["lofi"], now=100.0))

    assert verdicts["lofi"].fault == "composer_exit"
    # A dead composer has no relay path to hand over, so it is started, not swapped.
    assert runtime.starts == ["lofi"]
    assert runtime.restarts == []


def test_watchdog_swaps_a_stalled_but_living_composer(repo: Path) -> None:
    runtime = FakeRuntime(progress=progress_block(5_000_000, 0.44))
    watchdog = make_watchdog(repo, runtime)

    async def scenario() -> None:
        await watchdog.poll_once(["lofi"], now=0.0)
        await watchdog.poll_once(["lofi"], now=60.0)
        await watchdog.poll_once(["lofi"], now=90.0)

    asyncio.run(scenario())
    assert runtime.restarts == ["lofi"]


def test_a_stopped_channel_is_never_restarted(repo: Path) -> None:
    runtime = FakeRuntime(composer=ABSENT, liquidsoap=ContainerInfo(name="lofi-liquidsoap"))
    watchdog = make_watchdog(repo, runtime)
    asyncio.run(watchdog.poll_once(["lofi"], now=500.0))
    assert runtime.restarts == [] and runtime.starts == []


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------


def test_backoff_ascends_and_never_hot_loops(repo: Path) -> None:
    runtime = FakeRuntime(composer=EXITED_OK)
    watchdog = make_watchdog(repo, runtime)
    schedule = watchdog.settings.restart_backoff_seconds

    async def scenario() -> None:
        now = 0.0
        for _ in range(60):
            await watchdog.poll_once(["lofi"], now=now)
            now += 1.0

    asyncio.run(scenario())
    watch = watchdog.channels["lofi"]

    # 60 one-second polls against a permanently dead composer must not produce
    # 60 restarts: the settle window and the ladder hold it back.
    assert 1 < watch.restarts < 6
    assert watch.backoff_index == watch.restarts
    assert watchdog.backoff(0) == schedule[0]
    assert watchdog.backoff(99) == schedule[-1]
    assert schedule == sorted(schedule)


def test_recovery_resets_the_ladder(repo: Path) -> None:
    runtime = FakeRuntime(composer=EXITED_OK)
    watchdog = make_watchdog(repo, runtime)

    async def scenario() -> None:
        await watchdog.poll_once(["lofi"], now=0.0)
        runtime.composer = COMPOSER
        runtime.progress = progress_block(1_000_000, 1.0)
        for step in range(1, 6):
            runtime.progress = progress_block(step * 5_000_000, 1.0)
            await watchdog.poll_once(["lofi"], now=100.0 + step * 5.0)

    asyncio.run(scenario())
    assert watchdog.channels["lofi"].backoff_index == 0


def test_a_fault_emits_a_watchdog_event(repo: Path) -> None:
    hub = EventHub()
    runtime = FakeRuntime(composer=EXITED_OK)
    watchdog = make_watchdog(repo, runtime, hub)

    async def scenario() -> list[str]:
        async with hub.subscribe() as subscriber:
            await watchdog.poll_once(["lofi"], now=10.0)
            frames = []
            while not subscriber.queue.empty():
                frames.append(subscriber.queue.get_nowait())
            return frames

    frames = asyncio.run(scenario())
    assert any("watchdog.event" in frame and "restarting" in frame for frame in frames)
    assert any("channel.status" in frame and "failed" in frame for frame in frames)


def test_one_broken_channel_does_not_stop_the_others(repo: Path) -> None:
    class Exploding(FakeRuntime):
        async def containers(self, name: str):
            if name == "broken":
                raise RuntimeError("docker is on fire")
            return await super().containers(name)

    runtime = Exploding(progress=progress_block(1_000_000, 1.0))
    watchdog = make_watchdog(repo, runtime)
    verdicts = asyncio.run(watchdog.poll_once(["broken", "lofi"], now=0.0))
    assert "lofi" in verdicts and "broken" not in verdicts
