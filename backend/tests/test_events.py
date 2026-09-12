"""SSE hub: event shapes, throttling and slow-consumer handling."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ambient.events import (
    CHANNEL_PROGRESS,
    CHANNEL_STATUS,
    EVENT_NAMES,
    WATCHDOG_EVENT,
    EventHub,
)
from ambient.api.deps import run_visualizer_action
from ambient.supervisor import VisualizerAction


def run(coro):
    return asyncio.run(coro)


def parse(frame: str) -> tuple[str, dict]:
    lines = frame.strip().splitlines()
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])


def test_event_names_match_the_contract() -> None:
    assert EVENT_NAMES == {
        "channel.status",
        "channel.progress",
        "channel.track",
        "channel.slide",
        "channel.visualization",
        "watchdog.event",
        "capacity.warning",
        "job.progress",
        # Additive: media upload finished, the library changed. Pending in
        # docs/contracts/rest-api.md.
        "media.uploaded",
    }


def test_every_event_carries_channel_and_iso_at() -> None:
    async def scenario() -> tuple[str, dict]:
        hub = EventHub()
        async with hub.subscribe() as subscriber:
            await hub.publish(
                CHANNEL_STATUS, {"state": "running", "health": "healthy", "speed": 1.0},
                channel="lofi",
            )
            return parse(subscriber.queue.get_nowait())

    name, data = run(scenario())
    assert name == "channel.status"
    assert data["channel"] == "lofi"
    assert data["at"].endswith("Z")
    assert data["state"] == "running"
    assert data["speed"] == 1.0


def test_a_system_wide_event_carries_a_null_channel() -> None:
    async def scenario() -> dict:
        hub = EventHub()
        async with hub.subscribe() as subscriber:
            await hub.publish(WATCHDOG_EVENT, {"event": "restarted"})
            return parse(subscriber.queue.get_nowait())[1]

    assert run(scenario())["channel"] is None


def test_progress_is_throttled_to_one_hertz_per_channel() -> None:
    async def scenario() -> tuple[list[bool], int]:
        hub = EventHub()
        async with hub.subscribe() as subscriber:
            sent = [
                await hub.publish(CHANNEL_PROGRESS, {"speed": 1.0}, channel="lofi")
                for _ in range(5)
            ]
            # A different channel has its own budget.
            sent.append(await hub.publish(CHANNEL_PROGRESS, {"speed": 1.0}, channel="rain"))
            return sent, subscriber.queue.qsize()

    sent, delivered = run(scenario())
    assert sent[0] is True
    assert sent[1:5] == [False, False, False, False]
    assert sent[5] is True
    assert delivered == 2


def test_status_events_are_never_throttled() -> None:
    async def scenario() -> int:
        hub = EventHub()
        async with hub.subscribe() as subscriber:
            for _ in range(5):
                await hub.publish(CHANNEL_STATUS, {"state": "running"}, channel="lofi")
            return subscriber.queue.qsize()

    assert run(scenario()) == 5


def test_a_slow_consumer_is_dropped_not_buffered() -> None:
    async def scenario() -> tuple[bool, int, int]:
        hub = EventHub(queue_size=3)
        async with hub.subscribe() as subscriber:
            for _ in range(10):
                await hub.publish(CHANNEL_STATUS, {"state": "running"}, channel="lofi")
            return subscriber.closed.is_set(), subscriber.queue.qsize(), hub.subscriber_count

    closed, queued, remaining = run(scenario())
    assert closed is True
    assert queued == 3  # bounded: it never grew past the queue size
    assert remaining == 0


def test_stream_yields_a_comment_then_events() -> None:
    async def scenario() -> list[str]:
        hub = EventHub()
        frames: list[str] = []
        async with hub.subscribe() as subscriber:
            stream = hub.stream(subscriber, keepalive=0.05)
            frames.append(await anext(stream))
            await hub.publish(CHANNEL_STATUS, {"state": "running"}, channel="lofi")
            frames.append(await anext(stream))
            await stream.aclose()
        return frames

    frames = run(scenario())
    assert frames[0].startswith(":")
    assert parse(frames[1])[0] == "channel.status"


def test_unknown_event_names_are_rejected() -> None:
    async def scenario() -> None:
        await EventHub().publish("channel.explode", {})

    with pytest.raises(ValueError, match="unknown event name"):
        run(scenario())


def test_visualizer_queued_and_superseded_events_have_complete_state() -> None:
    async def scenario() -> list[dict]:
        hub = EventHub()
        state = SimpleNamespace(events=hub)

        async def superseded() -> VisualizerAction:
            return VisualizerAction("recreate", 7, False, "lofi-visualizer", "")

        async with hub.subscribe() as subscriber:
            await run_visualizer_action(
                state,
                "lofi",
                superseded(),
                "recreate",
                7,
                event_data={"active": "showwaves-classic"},
            )
            return [parse(subscriber.queue.get_nowait())[1] for _ in range(2)]

    queued, superseded = run(scenario())
    for event in (queued, superseded):
        assert {"generation", "applied", "error", "detail", "state"} <= event.keys()
        assert event["generation"] == 7
        assert event["active"] == "showwaves-classic"
    assert queued["state"] == "queued"
    assert superseded["state"] == "superseded"
    assert superseded["applied"] is False


def test_visualizer_failure_event_keeps_the_reserved_generation() -> None:
    async def scenario() -> dict:
        hub = EventHub()
        state = SimpleNamespace(events=hub)

        async def fail() -> None:
            raise RuntimeError("scripted failure")

        async with hub.subscribe() as subscriber:
            await run_visualizer_action(state, "lofi", fail(), "start", 11)
            subscriber.queue.get_nowait()
            return parse(subscriber.queue.get_nowait())[1]

    failed = run(scenario())
    assert failed["generation"] == 11
    assert failed["applied"] is False
    assert failed["error"] == "visualizer_action_failed"
    assert failed["detail"] == "scripted failure"
    assert failed["state"] == "failed"
