"""Per-channel preset schedules.

Resolution order, most specific first: `date` -> `days` + `when` -> `when` ->
the channel default. First match wins.

Times are local to the channel's `timezone` and compared as wall clock, which
is what makes DST safe rather than something to discover later:

* On a spring-forward day a window inside the skipped hour simply never
  matches — the rule does not fire, and nothing raises.
* On a fall-back day a window inside the repeated hour matches twice. Applying
  a preset is idempotent, and the scheduler applies only when the resolved
  preset differs from the one currently applied, so the second pass is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

from .models import Schedule, ScheduleRule

LOG = logging.getLogger("ambient.scheduler")

DEFAULT_TICK_SECONDS = 20.0

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_window(window: str) -> tuple[time, time]:
    start_text, _, end_text = window.partition("-")
    start_h, start_m = (int(part) for part in start_text.split(":"))
    end_h, end_m = (int(part) for part in end_text.split(":"))
    return time(start_h, start_m), time(end_h, end_m)


def window_contains(window: str, moment: time) -> bool:
    """End-exclusive, and a start after the end wraps past midnight."""
    start, end = _parse_window(window)
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def rule_matches(rule: ScheduleRule, local: datetime) -> bool:
    if rule.date is not None and rule.date != local.date().isoformat():
        return False
    if rule.days is not None and _WEEKDAYS[local.weekday()] not in [d.value for d in rule.days]:
        return False
    if rule.when is not None and not window_contains(rule.when, local.time()):
        return False
    return True


def _specificity(rule: ScheduleRule) -> int:
    if rule.date is not None:
        return 0
    if rule.days is not None and rule.when is not None:
        return 1
    if rule.when is not None:
        return 2
    return 3


def local_now(schedule: Schedule, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    moment = datetime.now(zone) if now is None else now
    if moment.tzinfo is None:
        # A naive datetime is read as already-local wall clock, not as UTC.
        return moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def resolve(
    schedule: Schedule, default: str | None, now: datetime | None = None
) -> tuple[str | None, ScheduleRule | None]:
    """The preset that should be active, and the rule that chose it."""
    local = local_now(schedule, now)
    for tier in (0, 1, 2):
        for rule in schedule.rules:
            if _specificity(rule) == tier and rule_matches(rule, local):
                return rule.preset, rule
    return default, None


@dataclass
class ChannelSchedule:
    name: str
    schedule: Schedule
    default: str | None
    applied: str | None = None


ApplyFn = Callable[[str, str], Awaitable[None]]
DiscoverFn = Callable[[], Iterable[ChannelSchedule]]


@dataclass
class Scheduler:
    """Evaluates on a timer and on config change; applies only on a change."""

    discover: DiscoverFn
    apply: ApplyFn
    tick_seconds: float = DEFAULT_TICK_SECONDS
    applied: dict[str, str | None] = field(default_factory=dict)
    enabled: bool = True
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stopping: asyncio.Event | None = field(default=None, repr=False)

    def forget(self, name: str) -> None:
        self.applied.pop(name, None)

    async def tick(self, now: datetime | None = None) -> dict[str, str]:
        """One evaluation pass. Returns the presets it actually applied."""
        applied: dict[str, str] = {}
        for entry in self.discover():
            try:
                wanted, rule = resolve(entry.schedule, entry.default, now)
            except Exception:  # a broken schedule must not stop the others
                LOG.exception("channel %s: schedule evaluation failed", entry.name)
                continue
            current = self.applied.get(entry.name, entry.applied)
            if wanted is None or wanted == current:
                self.applied[entry.name] = current if wanted is None else wanted
                continue
            try:
                await self.apply(entry.name, wanted)
            except Exception as exc:
                # A DST-skipped or otherwise unapplicable rule must not raise
                # out of the loop; the next tick tries again.
                LOG.warning("channel %s: could not apply preset %r: %s", entry.name, wanted, exc)
                continue
            self.applied[entry.name] = wanted
            applied[entry.name] = wanted
            LOG.info(
                "channel %s: preset %r applied by rule %r",
                entry.name,
                wanted,
                rule.name if rule else "default",
            )
        return applied

    async def run(self) -> None:
        self._stopping = asyncio.Event()
        while not self._stopping.is_set():
            if self.enabled:
                try:
                    await self.tick()
                except Exception:  # pragma: no cover - defensive
                    LOG.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                continue

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(), name="ambient-scheduler")
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
