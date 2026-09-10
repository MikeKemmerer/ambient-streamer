"""The public station directory.

Unauthenticated on purpose: it lives on the LAN HLS port, which MediaMTX already
served without a token. So the interesting tests are about what it must NOT do —
no control surface, no stream keys, no expensive work an anonymous caller could
amplify, and never the program path, which is the YouTube feed.

"Active" is taken from the relay, not from configuration. A directory that lists
a station you cannot tune to is worse than an empty one.
"""

from __future__ import annotations

import pytest

from tests.conftest import AUTH


@pytest.fixture
def relay(monkeypatch):
    """Stand in for the relay's path listing."""
    paths: dict[str, dict] = {}

    async def fake_paths(self):
        return dict(paths)

    from ambient.main import AppState

    monkeypatch.setattr(AppState, "relay_paths", fake_paths)
    return paths


def ready(*names: str) -> dict[str, dict]:
    return {name: {"name": name, "ready": True} for name in names}


def test_the_directory_needs_no_token(api, relay) -> None:
    """It is what a listener on the LAN opens; a token would defeat the point."""
    client, _state = api
    relay.update(ready("lofi/video"))

    response = client.get("/directory")

    assert response.status_code == 200
    assert "lofi" in response.text


def test_only_streams_the_relay_says_are_publishing_are_listed(api, relay) -> None:
    client, _state = api
    relay.update(ready("lofi/video"))

    body = client.get("/directory.json", headers=AUTH).json()

    assert [s["name"] for s in body["stations"]] == ["lofi"]
    assert [f["kind"] for f in body["stations"][0]["feeds"]] == ["video"]


def test_a_configured_but_idle_channel_is_not_listed(api, relay) -> None:
    client, _state = api
    relay["lofi/video"] = {"name": "lofi/video", "ready": False}

    assert client.get("/directory.json", headers=AUTH).json()["stations"] == []


def test_the_program_feed_is_never_listed(api, relay) -> None:
    """It is the YouTube feed and the one path the publisher hook reads."""
    client, _state = api
    relay.update(ready("lofi"))

    body = client.get("/directory.json", headers=AUTH).json()

    assert body["stations"] == []


def test_the_operator_preview_is_not_a_station(api, relay) -> None:
    """A directory of stations, not of tooling."""
    client, _state = api
    relay.update(ready("lofi/preview"))

    assert client.get("/directory.json", headers=AUTH).json()["stations"] == []


def test_both_internal_renditions_are_offered(api, relay) -> None:
    client, _state = api
    relay.update(ready("lofi/video", "lofi/audio"))

    feeds = client.get("/directory.json", headers=AUTH).json()["stations"][0]["feeds"]

    assert [f["kind"] for f in feeds] == ["video", "audio only"]


def test_urls_are_absolute_and_point_at_the_host_the_listener_used(api, relay) -> None:
    """The relay's internal hostname resolves nowhere on a listener's machine."""
    client, _state = api
    relay.update(ready("lofi/video"))

    body = client.get(
        "/directory.json", headers={**AUTH, "Host": "192.168.1.10:8888"}
    ).json()

    assert body["stations"][0]["feeds"][0]["url"] == (
        "http://192.168.1.10:8888/lofi/video/index.m3u8"
    )


def test_an_unreachable_relay_reports_nothing_rather_than_everything(api, monkeypatch) -> None:
    """`relay_paths` returns None when it could not ask; that is not 'all down'."""
    from ambient.main import AppState

    async def unreachable(self):
        return None

    monkeypatch.setattr(AppState, "relay_paths", unreachable)
    client, _state = api

    response = client.get("/directory")

    assert response.status_code == 200
    assert "No local stations" in response.text


def test_no_secret_reaches_the_page(api, relay) -> None:
    client, state = api
    relay.update(ready("lofi/video"))

    text = client.get("/directory").text

    key = state.channel("lofi", resolve_media=False).env.stream_key.get_secret_value()
    assert key and key not in text
    assert state.token not in text
    assert "rtmp://" not in text


def test_a_hostile_channel_name_cannot_inject_markup(api, relay, monkeypatch) -> None:
    """Every value on this page is escaped; none of it is trusted."""
    client, state = api
    relay.update(ready("lofi/video"))

    channel = state.channel("lofi", resolve_media=False)
    monkeypatch.setattr(channel.config, "genre", '<script>alert(1)</script>')
    monkeypatch.setattr(
        type(state), "channel", lambda self, name, **kw: channel, raising=False
    )

    text = client.get("/directory").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_the_operator_ui_still_owns_the_backends_own_root(api, relay) -> None:
    """The directory is mapped onto `/` by the front door, not by the app."""
    client, _state = api
    relay.update(ready("lofi/video"))

    # The UI is not mounted in the test sandbox, so the assertion is simply that
    # the directory did not claim the root route for itself.
    assert client.get("/directory").status_code == 200
    assert client.get("/").status_code == 404
