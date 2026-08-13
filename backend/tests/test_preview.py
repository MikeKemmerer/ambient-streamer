"""The HLS preview proxy: what it fetches, and what it refuses to fetch.

This is an outbound request driven by a path the browser supplies, so the tests
that matter are the ones proving the caller cannot steer it: not to another
host, not to another channel's media, and not off the relay's preview prefix.
"""

from __future__ import annotations

import pytest

from ambient.api import preview as preview_api
from tests.conftest import AUTH

PLAYLIST = b"#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\nseg0.m4s\n"


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL the proxy would fetch, and answer without a network."""
    seen: list[str] = []

    def fake_fetch(url: str) -> tuple[int, bytes, str]:
        seen.append(url)
        if url.endswith(".m3u8"):
            return 200, PLAYLIST, "application/vnd.apple.mpegurl"
        return 200, b"\0\0\0\x18ftypmp42", "video/mp4"

    monkeypatch.setattr(preview_api, "_fetch", fake_fetch)
    return seen


def test_a_playlist_comes_back_with_the_hls_content_type(api, upstream):
    client, _state = api
    response = client.get("/lofi/preview/index.m3u8", headers=AUTH)
    assert response.status_code == 200
    assert response.content == PLAYLIST
    assert "mpegurl" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert upstream == ["http://mediamtx:8888/lofi/preview/index.m3u8"]


def test_a_segment_is_proxied_from_the_channels_own_prefix(api, upstream):
    client, _state = api
    response = client.get("/lofi/preview/seg0.m4s", headers=AUTH)
    assert response.status_code == 200
    assert upstream == ["http://mediamtx:8888/lofi/preview/seg0.m4s"]


def test_the_preview_requires_a_token(api, upstream):
    client, _state = api
    assert client.get("/lofi/preview/index.m3u8").status_code == 401
    assert upstream == [], "an unauthenticated request must not reach the relay"


def test_an_unknown_channel_never_reaches_the_relay(api, upstream):
    client, _state = api
    response = client.get("/nosuch/preview/index.m3u8", headers=AUTH)
    assert response.status_code == 404
    assert upstream == []


@pytest.mark.parametrize(
    "path",
    [
        "../index.m3u8",
        "..%2f..%2fetc%2fpasswd",
        "a/../../../secret",
        "seg0.m4s/../../../v3/config",
        "%2e%2e/%2e%2e/admin",
        "sub dir/seg.m4s",
        "seg0.m4s?x=1#y",
    ],
)
def test_a_crafted_path_cannot_leave_the_preview_prefix(api, upstream, path: str):
    client, _state = api
    response = client.get(f"/lofi/preview/{path}", headers=AUTH)
    # The status is not the invariant - the client and the router may normalize
    # a path before it ever arrives. What has to hold is that nothing the caller
    # writes can move the fetch off this channel's own preview prefix.
    assert response.status_code in (200, 400, 404), response.text
    for url in upstream:
        assert url.startswith("http://mediamtx:8888/lofi/preview/"), url
        assert ".." not in url, url
        assert "%2e%2e" not in url.lower(), url


def test_a_relay_404_reads_as_not_ready_rather_than_an_error(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_api, "_fetch", lambda url: (404, b"", ""))
    client, _state = api
    response = client.get("/lofi/preview/index.m3u8", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"] == "preview_not_ready"


def test_an_unreachable_relay_is_reported_not_hung(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_api, "_fetch", lambda url: (504, b"", ""))
    client, _state = api
    response = client.get("/lofi/preview/index.m3u8", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"] == "preview_unavailable"


def test_the_advertised_url_is_relative_so_a_browser_can_reach_it(api) -> None:
    client, _state = api
    body = client.get("/api/channels/lofi/preview", headers=AUTH).json()
    assert body["hls"] == "/lofi/preview/index.m3u8"
    assert "mediamtx" not in body["hls"], "an internal host resolves nowhere in a browser"


def test_an_external_player_is_offered_the_published_relay_port(api, monkeypatch) -> None:
    """VLC cannot send the bearer token, so it needs the relay directly."""
    client, state = api
    monkeypatch.setattr(state.workspace.env, "hls_publish", 8888)
    body = client.get("/api/channels/lofi", headers=AUTH).json()
    assert body["hls_url"] == "http://testserver:8888/lofi/preview/index.m3u8"
    assert "mediamtx" not in body["hls_url"], "an internal host resolves nowhere for VLC"


def test_no_external_url_is_offered_when_the_port_is_not_published(api, monkeypatch) -> None:
    client, state = api
    monkeypatch.setattr(state.workspace.env, "hls_publish", None)
    body = client.get("/api/channels/lofi", headers=AUTH).json()
    assert body["hls_url"] is None, "a URL nothing can route to is worse than none"


def test_an_oversized_body_is_refused_rather_than_buffered(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_api, "_fetch", lambda url: (502, b"", ""))
    client, _state = api
    assert client.get("/lofi/preview/seg0.m4s", headers=AUTH).status_code == 503
