"""Scheduler: resolution order, and the two DST consequences the contract names."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ambient.models import Schedule, ScheduleRule
from ambient.scheduler import ChannelSchedule, Scheduler, resolve, window_contains

LA = "America/Los_Angeles"

RULES = [
    ScheduleRule(name="morning", when="06:00-11:00", days=["Mon", "Tue", "Wed", "Thu", "Fri"], preset="warm-sunrise"),
    ScheduleRule(name="evening", when="18:00-23:00", preset="calm-ocean"),
    ScheduleRule(name="sunday", when="09:00-12:00", days=["Sun"], preset="orthodox-chant"),
    ScheduleRule(name="christmas", date="2026-12-25", preset="winter-quiet"),
]


def schedule(rules=None, timezone_name: str = LA) -> Schedule:
    return Schedule(timezone=timezone_name, rules=list(RULES if rules is None else rules))


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Windows and resolution order
# --------------------------------------------------------------------------


def test_windows_are_end_exclusive() -> None:
    from datetime import time

    assert window_contains("06:00-11:00", time(6, 0))
    assert window_contains("06:00-11:00", time(10, 59))
    assert not window_contains("06:00-11:00", time(11, 0))


def test_a_window_whose_start_is_after_its_end_wraps_past_midnight() -> None:
    from datetime import time

    assert window_contains("22:00-02:00", time(23, 30))
    assert window_contains("22:00-02:00", time(1, 0))
    assert not window_contains("22:00-02:00", time(12, 0))


def test_date_beats_days_and_when() -> None:
    # 2026-12-25 is a Friday inside the morning window.
    preset, rule = resolve(schedule(), None, utc(2026, 12, 25, 15))
    assert preset == "winter-quiet"
    assert rule is not None and rule.name == "christmas"


def test_days_plus_when_beats_when_alone() -> None:
    # A Sunday at 10:00 local matches both `sunday` and nothing else.
    preset, rule = resolve(schedule(), None, utc(2026, 8, 9, 17))
    assert preset == "orthodox-chant"
    assert rule is not None and rule.name == "sunday"


def test_when_alone_applies_when_no_day_rule_matches() -> None:
    preset, rule = resolve(schedule(), None, utc(2026, 8, 9, 3))  # Sat 20:00 local
    assert preset == "calm-ocean"
    assert rule is not None and rule.name == "evening"


def test_the_channel_default_wins_when_nothing_matches() -> None:
    preset, rule = resolve(schedule(), "deep-space", utc(2026, 8, 8, 10))  # Sat 03:00 local
    assert preset == "deep-space"
    assert rule is None


def test_first_match_wins_within_a_tier() -> None:
    rules = [
        ScheduleRule(name="first", when="00:00-23:59", preset="one"),
        ScheduleRule(name="second", when="00:00-23:59", preset="two"),
    ]
    assert resolve(schedule(rules), None, utc(2026, 8, 8, 20))[0] == "one"


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------


def test_a_rule_inside_the_skipped_hour_never_fires_and_never_raises() -> None:
    """2026-03-08: America/Los_Angeles jumps 02:00 -> 03:00."""
    rules = [ScheduleRule(name="gap", when="02:15-02:45", preset="ghost")]
    moment = utc(2026, 3, 8, 8)  # 00:00 local
    matched = []
    for _ in range(360):  # six hours of local wall clock, one minute at a time
        preset, _rule = resolve(schedule(rules), None, moment)
        if preset == "ghost":
            matched.append(moment)
        moment += timedelta(minutes=1)
    assert matched == []


def test_a_rule_inside_the_repeated_hour_fires_twice_without_raising() -> None:
    """2026-11-01: America/Los_Angeles repeats 01:00-02:00."""
    rules = [ScheduleRule(name="fold", when="01:00-02:00", preset="twice")]
    first = resolve(schedule(rules), None, utc(2026, 11, 1, 8, 30))  # 01:30 PDT
    second = resolve(schedule(rules), None, utc(2026, 11, 1, 9, 30))  # 01:30 PST
    assert first[0] == "twice"
    assert second[0] == "twice"


def test_applying_the_same_preset_twice_is_a_no_op() -> None:
    rules = [ScheduleRule(name="fold", when="01:00-02:00", preset="twice")]
    applied: list[str] = []

    async def apply(name: str, preset: str) -> None:
        applied.append(preset)

    scheduler = Scheduler(
        discover=lambda: [ChannelSchedule("lofi", schedule(rules), None)],
        apply=apply,
    )

    async def scenario() -> None:
        await scheduler.tick(utc(2026, 11, 1, 8, 30))
        await scheduler.tick(utc(2026, 11, 1, 9, 30))  # the same wall clock, one hour later

    asyncio.run(scenario())
    assert applied == ["twice"]


def test_a_preset_that_cannot_be_applied_does_not_raise_out_of_the_tick() -> None:
    rules = [ScheduleRule(name="all-day", when="00:00-23:59", preset="missing")]

    async def apply(name: str, preset: str) -> None:
        raise ValueError("preset 'missing' does not exist")

    scheduler = Scheduler(
        discover=lambda: [ChannelSchedule("lofi", schedule(rules), None)], apply=apply
    )
    applied = asyncio.run(scheduler.tick(utc(2026, 8, 8, 20)))
    assert applied == {}
    # The next tick retries rather than latching the failure.
    assert scheduler.applied.get("lofi") is None


def test_only_a_change_triggers_an_apply() -> None:
    rules = [ScheduleRule(name="evening", when="18:00-23:00", preset="calm-ocean")]
    applied: list[str] = []

    async def apply(name: str, preset: str) -> None:
        applied.append(preset)

    scheduler = Scheduler(
        discover=lambda: [ChannelSchedule("lofi", schedule(rules), None)], apply=apply
    )

    async def scenario() -> None:
        for minute in range(0, 50, 10):
            await scheduler.tick(utc(2026, 8, 8, 3, minute))

    asyncio.run(scenario())
    assert applied == ["calm-ocean"]


def test_a_channel_already_on_the_resolved_preset_is_left_alone() -> None:
    rules = [ScheduleRule(name="evening", when="18:00-23:00", preset="calm-ocean")]
    applied: list[str] = []

    async def apply(name: str, preset: str) -> None:
        applied.append(preset)

    scheduler = Scheduler(
        discover=lambda: [ChannelSchedule("lofi", schedule(rules), None, applied="calm-ocean")],
        apply=apply,
    )
    asyncio.run(scheduler.tick(utc(2026, 8, 8, 3)))
    assert applied == []


def test_an_unknown_timezone_is_rejected_at_config_load() -> None:
    with pytest.raises(Exception):
        Schedule(timezone="Mars/Olympus", rules=[])
