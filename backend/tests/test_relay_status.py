"""Relay status must not depend on which channel the operator has selected.

Listing channels used to hard-code "disconnected" and only ask the relay on the
detail request, so an unselected channel reported the YouTube leg as down while
it was publishing. A wrong answer is worse than no answer here, because it is
indistinguishable from a real outage.
"""

from __future__ import annotations

import pytest

from tests.conftest import AUTH

READY = {
    "items": [
        {"name": "lofi", "ready": True},
        {"name": "lofi/preview", "ready": True},
    ]
}


def _summary(client, name: str = "lofi") -> dict:
    body = client.get("/api/channels", headers=AUTH).json()
    return next(c for c in body["channels"] if c["name"] == name)


def test_the_list_reports_the_same_relay_state_as_the_detail(api, monkeypatch) -> None:
    client, state = api

    async def paths() -> dict:
        return {item["name"]: item for item in READY["items"]}

    async def one(path: str) -> dict:
        return {item["name"]: item for item in READY["items"]}.get(path)

    monkeypatch.setattr(state, "relay_paths", paths)
    monkeypatch.setattr(state, "relay_path", one)

    listed = _summary(client)
    detailed = client.get("/api/channels/lofi", headers=AUTH).json()

    assert listed["rtmp"] == "connected"
    assert listed["rtmp"] == detailed["rtmp"], "selecting a channel must not change its status"
    assert listed["hls"] == detailed["hls"]


def test_a_genuinely_absent_path_still_reads_disconnected(api, monkeypatch) -> None:
    client, state = api

    async def paths() -> dict:
        return {}

    monkeypatch.setattr(state, "relay_paths", paths)
    assert _summary(client)["rtmp"] == "disconnected"


def test_an_unreachable_relay_is_unknown_not_disconnected(api, monkeypatch) -> None:
    client, state = api

    async def paths() -> None:
        return None

    monkeypatch.setattr(state, "relay_paths", paths)
    summary = _summary(client)
    assert summary["rtmp"] == "unknown", "not asking is not the same as being down"
    assert summary["hls"] == "unknown"


def test_the_whole_list_costs_one_relay_call(api, monkeypatch) -> None:
    client, state = api
    calls: list[str] = []

    async def paths() -> dict:
        calls.append("list")
        return {item["name"]: item for item in READY["items"]}

    async def one(path: str) -> None:
        calls.append(f"get:{path}")
        return None

    monkeypatch.setattr(state, "relay_paths", paths)
    monkeypatch.setattr(state, "relay_path", one)

    client.get("/api/channels", headers=AUTH)
    assert calls == ["list"], f"listing should not fan out per channel: {calls}"
