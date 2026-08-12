"""Health detection and recovery.

A liveness check cannot find either dangerous failure, both measured:

* A **stalled producer kills nothing.** FFmpeg stays alive, `speed` falls to
  0.44x and YouTube starves. Detected by `out_time` failing to advance against
  wallclock, and by `speed` sustained below `min_speed`.
* A **dead producer makes FFmpeg exit rc=0**, indistinguishable from clean
  completion. So **any** composer exit is a fault regardless of exit code.

`drop_frames` and `dup_frames` are deliberately unused: both stayed at 0
through the burst-pacing failure, because the `fps` filter's duplication is
internal and never reaches the muxer's counters.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .config import Workspace
from .events import CHANNEL_PROGRESS, CHANNEL_STATUS, WATCHDOG_EVENT, EventHub
from .models import ChannelState, Health

LOG = logging.getLogger("ambient.watchdog")

# Long enough that a freshly launched composer is not called stalled: the
# producer is throttled to ~1.5 fps for 4-6 s while FFmpeg initialises, and
# that offset is benign.
STARTUP_GRACE_SECONDS = 45.0
# After a restart, do not judge or restart again until the replacement settled.
SETTLE_SECONDS = 30.0


@dataclass(frozen=True)
class ProgressSample:
    """One complete `-progress` block."""

    frame: int = 0
    fps: float = 0.0
    bitrate_kbps: float = 0.0
    total_size: int = 0
    out_time_us: int = 0
    speed: float = 0.0
    progress: str = "continue"

    @property
    def out_time_seconds(self) -> float:
        return self.out_time_us / 1_000_000.0


def _to_float(value: str) -> float:
    try:
        return float(value.strip().rstrip("xX"))
    except ValueError:
        return 0.0


def parse_progress(text: str) -> ProgressSample | None:
    """Parse the last complete block of FFmpeg `-progress` output.

    The file is appended to forever, so callers tail it; a truncated leading
    block is discarded rather than half-parsed.
    """
    if not text:
        return None
    fields: dict[str, str] = {}
    blocks: list[dict[str, str]] = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        fields[key] = value.strip()
        if key == "progress":
            blocks.append(fields)
            fields = {}
    if not blocks:
        return None
    block = blocks[-1]

    # out_time_ms is FFmpeg's misnomer for microseconds; out_time_us is preferred.
    raw = block.get("out_time_us") or block.get("out_time_ms") or "0"
    try:
        micros = int(raw)
    except ValueError:
        micros = 0
    bitrate = block.get("bitrate", "0")
    return ProgressSample(
        frame=int(_to_float(block.get("frame", "0"))),
        fps=_to_float(block.get("fps", "0")),
        bitrate_kbps=_to_float(bitrate.replace("kbits/s", "")) if "kbits" in bitrate else 0.0,
        total_size=int(_to_float(block.get("total_size", "0"))),
        out_time_us=micros,
        speed=_to_float(block.get("speed", "0")),
        progress=block.get("progress", "continue"),
    )


@dataclass(frozen=True)
class Verdict:
    state: ChannelState
    health: Health
    fault: str | None = None
    detail: str = ""
    sample: ProgressSample | None = None

    @property
    def faulty(self) -> bool:
        return self.fault is not None


@dataclass
class ChannelHealth:
    """Per-channel evaluator. Pure given a clock, so it is directly testable."""

    name: str
    min_speed: float = 0.97
    stall_seconds: float = 15.0
    startup_grace: float = STARTUP_GRACE_SECONDS

    observed_at: float | None = None
    last_out_time_us: int | None = None
    last_advance_at: float | None = None
    slow_since: float | None = None
    running_since: float | None = None

    def reset(self) -> None:
        self.observed_at = None
        self.last_out_time_us = None
        self.last_advance_at = None
        self.slow_since = None
        self.running_since = None

    def observe_stopped(self) -> Verdict:
        self.reset()
        return Verdict(ChannelState.STOPPED, Health.DISCONNECTED)

    def observe_exit(self, exit_code: int | None) -> Verdict:
        """Any composer exit is a fault, rc=0 included."""
        self.reset()
        return Verdict(
            ChannelState.FAILED,
            Health.DISCONNECTED,
            fault="composer_exit",
            detail=(
                f"composer exited rc={exit_code}; a dead producer makes FFmpeg "
                "exit 0, so every exit is a fault"
            ),
        )

    def observe(self, now: float, sample: ProgressSample | None) -> Verdict:
        if self.running_since is None:
            self.running_since = now
        starting = now - self.running_since < self.startup_grace

        if sample is None:
            if starting:
                return Verdict(ChannelState.STARTING, Health.HEALTHY, detail="no progress yet")
            return Verdict(
                ChannelState.DEGRADED,
                Health.STALLED,
                fault="no_progress",
                detail="composer is running but wrote no progress block",
            )

        advanced = self.last_out_time_us is None or sample.out_time_us > self.last_out_time_us
        if advanced:
            self.last_advance_at = now
        elif self.last_advance_at is None:
            self.last_advance_at = now
        self.last_out_time_us = sample.out_time_us
        self.observed_at = now

        if sample.speed and sample.speed < self.min_speed:
            if self.slow_since is None:
                self.slow_since = now
        else:
            self.slow_since = None

        stalled_for = 0.0 if self.last_advance_at is None else now - self.last_advance_at
        if stalled_for >= self.stall_seconds and not starting:
            return Verdict(
                ChannelState.DEGRADED,
                Health.STALLED,
                fault="out_time_stalled",
                detail=(
                    f"out_time held at {sample.out_time_seconds:.2f}s for "
                    f"{stalled_for:.1f}s of wallclock"
                ),
                sample=sample,
            )

        slow_for = 0.0 if self.slow_since is None else now - self.slow_since
        if slow_for >= self.stall_seconds and not starting:
            return Verdict(
                ChannelState.DEGRADED,
                Health.STARVING,
                fault="speed_below_minimum",
                detail=(
                    f"speed {sample.speed:.3f}x below {self.min_speed:.2f}x for "
                    f"{slow_for:.1f}s; YouTube is starving"
                ),
                sample=sample,
            )

        if starting and sample.out_time_us == 0:
            return Verdict(ChannelState.STARTING, Health.HEALTHY, sample=sample)
        return Verdict(ChannelState.RUNNING, Health.HEALTHY, sample=sample)


class ChannelRuntime(Protocol):
    """What the watchdog needs from the supervisor."""

    async def containers(self, name: str): ...

    async def read_run_file(self, name: str, filename: str, *, tail_bytes: int = ...) -> str: ...

    async def restart(self, name: str) -> dict[str, object]: ...

    async def start(self, name: str): ...


@dataclass
class ChannelWatch:
    health: ChannelHealth
    backoff_index: int = 0
    next_restart_allowed_at: float = 0.0
    restarts: int = 0
    last_verdict: Verdict | None = None
    settle_until: float = 0.0
    consecutive_healthy: int = 0


@dataclass
class Watchdog:
    """Polls every channel, restarts faults with exponential backoff.

    The same channel is retried rather than skipped, and the delay grows
    through `watchdog.restart_backoff_seconds`: a tight restart loop against
    YouTube looks like abuse.
    """

    workspace: Workspace
    supervisor: ChannelRuntime
    events: EventHub | None = None
    channels: dict[str, ChannelWatch] = field(default_factory=dict)
    latest: dict[str, Verdict] = field(default_factory=dict)
    cpu: dict[str, float] = field(default_factory=dict)
    enabled: bool = True
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stopping: asyncio.Event | None = field(default=None, repr=False)

    @property
    def settings(self):
        return self.workspace.ambient.watchdog

    def _watch(self, name: str) -> ChannelWatch:
        watch = self.channels.get(name)
        if watch is None:
            watch = ChannelWatch(
                health=ChannelHealth(
                    name=name,
                    min_speed=self.settings.min_speed,
                    stall_seconds=self.settings.stall_seconds,
                )
            )
            self.channels[name] = watch
        return watch

    def backoff(self, index: int) -> float:
        schedule = self.settings.restart_backoff_seconds
        return float(schedule[min(index, len(schedule) - 1)])

    def forget(self, name: str) -> None:
        self.channels.pop(name, None)
        self.latest.pop(name, None)

    # ------------------------------------------------------------- one cycle

    async def evaluate(self, name: str, now: float) -> Verdict:
        watch = self._watch(name)
        containers = await self.supervisor.containers(name)
        composer = containers.composer

        if not composer.exists:
            verdict = watch.health.observe_stopped()
        elif not composer.running:
            verdict = watch.health.observe_exit(composer.exit_code)
        else:
            text = await self.supervisor.read_run_file(name, "progress")
            verdict = watch.health.observe(now, parse_progress(text))

        previous = watch.last_verdict
        watch.last_verdict = verdict
        self.latest[name] = verdict

        if self.events is not None:
            if (
                previous is None
                or previous.state is not verdict.state
                or previous.health is not verdict.health
            ):
                await self.events.publish(
                    CHANNEL_STATUS,
                    {
                        "state": verdict.state.value,
                        "health": verdict.health.value,
                        "speed": verdict.sample.speed if verdict.sample else 0.0,
                    },
                    channel=name,
                )
            if verdict.sample is not None:
                await self.events.publish(
                    CHANNEL_PROGRESS,
                    {
                        "speed": verdict.sample.speed,
                        "fps": verdict.sample.fps,
                        "bitrate_kbps": verdict.sample.bitrate_kbps,
                        "uptime_seconds": round(verdict.sample.out_time_seconds, 1),
                    },
                    channel=name,
                )
        return verdict

    async def act(self, name: str, verdict: Verdict, now: float) -> bool:
        """Restart a faulty channel when its backoff has elapsed."""
        watch = self._watch(name)

        if not verdict.faulty:
            watch.consecutive_healthy += 1
            # Reset the ladder only once a channel has genuinely held up.
            if watch.consecutive_healthy >= 3 and watch.backoff_index:
                LOG.info("channel %s recovered; backoff reset", name)
                watch.backoff_index = 0
            return False

        watch.consecutive_healthy = 0
        if verdict.state is ChannelState.STOPPED:
            return False  # deliberately stopped, not a fault to recover from
        if now < watch.settle_until or now < watch.next_restart_allowed_at:
            return False

        delay = self.backoff(watch.backoff_index)
        watch.restarts += 1
        await self._emit(
            name,
            "restarting",
            fault=verdict.fault,
            detail=verdict.detail,
            attempt=watch.restarts,
            backoff_seconds=delay,
        )
        try:
            if verdict.state is ChannelState.FAILED:
                # Nothing is publishing, so there is no relay path to hand over.
                await self.supervisor.start(name)
            else:
                await self.supervisor.restart(name)
        except Exception as exc:  # a failed recovery must not kill the loop
            LOG.exception("channel %s: restart failed", name)
            await self._emit(name, "restart_failed", fault=verdict.fault, detail=str(exc))
        else:
            await self._emit(name, "restarted", fault=verdict.fault, attempt=watch.restarts)

        watch.health.reset()
        watch.backoff_index += 1
        # The NEXT restart waits; this one already happened. Never hot-loop.
        watch.next_restart_allowed_at = now + self.backoff(watch.backoff_index)
        watch.settle_until = now + SETTLE_SECONDS
        return True

    async def poll_once(self, names: list[str], now: float | None = None) -> dict[str, Verdict]:
        loop = asyncio.get_running_loop()
        moment = loop.time() if now is None else now
        verdicts: dict[str, Verdict] = {}
        for name in names:
            try:
                verdict = await self.evaluate(name, moment)
                verdicts[name] = verdict
                await self.act(name, verdict, moment)
            except Exception:  # one broken channel must not stop the others
                LOG.exception("channel %s: watchdog cycle failed", name)
        for stale in set(self.channels) - set(names):
            self.forget(stale)
        return verdicts

    async def _emit(self, name: str, event: str, **data: object) -> None:
        LOG.info("channel %s: watchdog %s %s", name, event, data)
        if self.events is not None:
            await self.events.publish(WATCHDOG_EVENT, {"event": event, **data}, channel=name)

    # ------------------------------------------------------------- the loop

    async def run(self, discover) -> None:
        self._stopping = asyncio.Event()
        interval = self.settings.poll_seconds
        while not self._stopping.is_set():
            if self.enabled:
                try:
                    await self.poll_once(list(discover()))
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("watchdog cycle failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def start(self, discover) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(discover), name="ambient-watchdog")
        return self._task

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
