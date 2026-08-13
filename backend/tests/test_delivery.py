"""Ingest settings: what a stopped channel may change, and what leaks.

These decide where the stream goes, so the endpoint is refused outright while
the channel is running rather than restarting it underneath a live broadcast.
The stream key is a credential: it goes in, it never comes back out.
"""

from __future__ import annotations

import pytest

from ambient.config import parse_env_file
from tests.conftest import AUTH, RUNNING_STATE


def env_of(repo, name: str = "lofi") -> dict:
    return parse_env_file(repo / "channels" / name / ".env")


def test_a_stopped_channel_accepts_new_ingest_settings(api, repo) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/delivery",
        headers=AUTH,
        json={"stream_key": "abcd-1234-efgh-5678", "rtmp_url": "rtmp://b.rtmp.youtube.com/live2",
              "encoder": "libx264", "fps": 24},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["changed"]) == {"stream_key", "rtmp_url", "encoder", "fps"}

    values = env_of(repo)
    assert values["YOUTUBE_STREAM_KEY"] == "abcd-1234-efgh-5678"
    assert values["CHANNEL_FPS"] == "24"
    assert values["CHANNEL_ENCODER"] == "libx264"


def test_the_stream_key_never_comes_back(api) -> None:
    client, _state = api
    secret = "supersecretkey1234"
    body = client.put(
        "/api/channels/lofi/delivery", headers=AUTH, json={"stream_key": secret}
    ).json()
    assert secret not in str(body)
    assert body["has_stream_key"] is True

    detail = client.get("/api/channels/lofi", headers=AUTH).text
    assert secret not in detail, "the key must not appear in channel detail"

    listing = client.get("/api/channels", headers=AUTH).text
    assert secret not in listing


def test_a_running_channel_is_refused_rather_than_restarted(api, docker, repo) -> None:
    client, state = api
    docker.states[state.supervisor.composer_container("lofi")] = RUNNING_STATE
    before = env_of(repo)
    response = client.put(
        "/api/channels/lofi/delivery", headers=AUTH, json={"fps": 24}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "channel_running"
    assert env_of(repo) == before, "a refused change must not touch .env"


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/live",
        "rtmp://host/live; rm -rf /",
        "rtmp://host/live\nYOUTUBE_STREAM_KEY=leak",
        "file:///etc/passwd",
        "rtmp://host/live$(id)",
        "",
    ],
)
def test_a_hostile_rtmp_url_is_refused(api, repo, url: str) -> None:
    client, _state = api
    before = env_of(repo)
    response = client.put("/api/channels/lofi/delivery", headers=AUTH, json={"rtmp_url": url})
    assert response.status_code == 400, response.text
    assert env_of(repo) == before


@pytest.mark.parametrize("key", ["has space", "semi;colon", "new\nline", "a" * 65, "quote'"])
def test_a_hostile_stream_key_is_refused(api, repo, key: str) -> None:
    client, _state = api
    before = env_of(repo)
    response = client.put("/api/channels/lofi/delivery", headers=AUTH, json={"stream_key": key})
    assert response.status_code == 400
    assert env_of(repo) == before


def test_an_out_of_range_fps_is_refused(api) -> None:
    client, _state = api
    assert client.put("/api/channels/lofi/delivery", headers=AUTH, json={"fps": 0}).status_code == 400
    assert client.put("/api/channels/lofi/delivery", headers=AUTH, json={"fps": 120}).status_code == 400


def test_an_unknown_encoder_is_refused(api) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/delivery", headers=AUTH, json={"encoder": "h264_magic"}
    )
    assert response.status_code == 400


def test_clearing_a_value_falls_back_to_the_global_default(api, repo) -> None:
    client, _state = api
    client.put("/api/channels/lofi/delivery", headers=AUTH, json={"fps": 24})
    assert env_of(repo)["CHANNEL_FPS"] == "24"

    body = client.put(
        "/api/channels/lofi/delivery", headers=AUTH, json={"clear_fps": True}
    ).json()
    assert env_of(repo)["CHANNEL_FPS"] == ""
    assert body["fps"] == 30, "an empty channel value inherits the global default"


def test_an_untouched_field_is_left_alone(api, repo) -> None:
    client, _state = api
    client.put("/api/channels/lofi/delivery", headers=AUTH, json={"stream_key": "keepme12"})
    client.put("/api/channels/lofi/delivery", headers=AUTH, json={"fps": 24})
    values = env_of(repo)
    assert values["YOUTUBE_STREAM_KEY"] == "keepme12", "editing fps must not lose the key"
    assert values["CHANNEL_MOUNT"], "unrelated keys survive the rewrite"


def test_the_detail_reports_what_was_asked_for_not_what_is_measured(api) -> None:
    client, _state = api
    client.put("/api/channels/lofi/delivery", headers=AUTH, json={"fps": 24})
    detail = client.get("/api/channels/lofi", headers=AUTH).json()
    assert detail["fps_requested"] == 24
    assert detail["fps"] == 0.0, "a stopped channel measures nothing"
    assert detail["rtmp_url"].startswith("rtmp")


def test_the_endpoint_requires_a_token(api) -> None:
    client, _state = api
    assert client.put("/api/channels/lofi/delivery", json={"fps": 24}).status_code == 401
