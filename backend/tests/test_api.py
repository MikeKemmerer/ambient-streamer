"""REST + SSE surface: auth, the endpoint table, and the error shape."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from ambient.main import StartupRefused, check_exposure, compare_token
from tests.conftest import AUTH, RUNNING_STATE, TOKEN, eventually

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_health_is_the_only_unauthenticated_endpoint(api) -> None:
    client, _state = api
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # It reports nothing an anonymous caller should not see.
    assert "token" not in response.text and TOKEN not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/channels",
        "/api/channels/lofi",
        "/api/system",
        "/api/capacity",
        "/api/plugins",
        "/api/presets",
        "/api/metrics",
        "/api/media/audio",
        "/api/media/images",
        "/api/channels/lofi/playlist",
        "/api/channels/lofi/images",
        "/api/channels/lofi/bumpers",
        "/api/logs?channel=lofi",
        "/api/events",
    ],
)
def test_every_other_endpoint_rejects_a_missing_token(api, path: str) -> None:
    client, _state = api
    response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {
        "error": "unauthorized",
        "detail": "a valid bearer token is required",
    }


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": "Bearer "},
        {"Authorization": ""},
    ],
)
def test_a_bad_token_is_rejected(api, header: dict) -> None:
    client, _state = api
    assert client.get("/api/channels", headers=header).status_code == 401


def test_mutating_endpoints_reject_a_missing_token(api) -> None:
    client, _state = api
    assert client.post("/api/channels/lofi/start").status_code == 401
    assert client.post("/api/channels/lofi/stop").status_code == 401
    assert client.post("/api/channels/lofi/restart").status_code == 401
    assert client.delete("/api/channels/lofi").status_code == 401
    assert client.put("/api/channels/lofi/playlist", json={"tracks": []}).status_code == 401


def test_the_token_comparison_rejects_an_unset_token() -> None:
    assert compare_token("", "") is False
    assert compare_token("anything", "") is False
    assert compare_token(TOKEN, TOKEN) is True


def test_the_app_refuses_to_start_open_on_a_non_loopback_address() -> None:
    with pytest.raises(StartupRefused, match="root-equivalent"):
        check_exposure("0.0.0.0", "")
    check_exposure("127.0.0.1", "")  # loopback without a token is allowed
    check_exposure("0.0.0.0", TOKEN)


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


def test_list_channels(api) -> None:
    client, _state = api
    body = client.get("/api/channels", headers=AUTH).json()
    assert [c["name"] for c in body["channels"]] == ["lofi"]
    assert body["channels"][0]["state"] == "stopped"


def test_get_channel_returns_the_dashboard_fields(api) -> None:
    client, state = api
    state.supervisor.runner.states["lofi-composer"] = RUNNING_STATE
    state.supervisor.runner.files["lofi-composer:/run/ambient/lofi/progress"] = (
        "out_time_us=84210000000\nspeed=1.000x\nfps=30.0\nbitrate=3000.0kbits/s\nprogress=continue\n"
    )
    state.supervisor.runner.files["lofi-composer:/run/ambient/lofi/now.json"] = json.dumps(
        {"current_track": "rain-loop.mp3", "next_track": "lofi-only.mp3", "current_slide": "forest.jpg"}
    )

    body = client.get("/api/channels/lofi", headers=AUTH).json()
    for field in (
        "name", "state", "uptime_seconds", "current_track", "next_track", "current_slide",
        "visualisation", "encoder", "encoder_requested", "fps", "speed", "bitrate_kbps",
        "cpu_cores", "liquidsoap_buffer", "rtmp", "hls", "health",
    ):
        assert field in body, field
    assert body["current_track"] == "rain-loop.mp3"
    assert body["speed"] == 1.0
    assert body["encoder_requested"] == "libx264"
    assert body["state"] in {"running", "starting"}


def test_an_unknown_channel_is_a_404(api) -> None:
    client, _state = api
    response = client.get("/api/channels/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_channel"


def test_a_hostile_channel_name_is_rejected(api) -> None:
    client, state = api
    # A traversal never reaches a handler: the router normalises it away.
    traversal = client.get("/api/channels/..%2F..%2Fetc", headers=AUTH)
    assert traversal.status_code == 404
    assert traversal.json()["error"] == "not_found"

    # One that does reach the handler is caught by the name pattern.
    for name in ("UPPER", "has%20space", "-leading"):
        response = client.get(f"/api/channels/{name}", headers=AUTH)
        assert response.status_code == 400, name
        assert response.json()["error"] == "invalid_channel_name"

    assert not any("etc" in " ".join(call) for call in state.supervisor.runner.calls)


def test_start_returns_202_and_passes_both_env_files(api, repo: Path) -> None:
    client, state = api
    response = client.post("/api/channels/lofi/start", headers=AUTH)
    assert response.status_code == 202
    assert response.json()["accepted"] is True

    compose = eventually(
        lambda: [c for c in state.supervisor.runner.compose_calls() if "up" in c]
    )
    assert compose, "no `docker compose up` was issued"
    env_files = [compose[0][i + 1] for i, t in enumerate(compose[0]) if t == "--env-file"]
    assert env_files == [str(repo / ".env"), str(repo / "channels" / "lofi" / ".env")]


def test_stop_and_restart_return_202(api) -> None:
    client, state = api
    assert client.post("/api/channels/lofi/stop", headers=AUTH).status_code == 202
    assert eventually(
        lambda: [c for c in state.supervisor.runner.compose_calls() if "down" in c]
    )
    restart = client.post("/api/channels/lofi/restart", headers=AUTH)
    assert restart.status_code == 202
    assert restart.json()["mode"] == "make-before-break"


def test_delete_refuses_while_containers_exist(api) -> None:
    client, state = api
    state.supervisor.runner.states["lofi-composer"] = RUNNING_STATE
    response = client.delete("/api/channels/lofi", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"] == "channel_running"


def test_delete_removes_a_stopped_channel(api, repo: Path) -> None:
    client, _state = api
    response = client.delete("/api/channels/lofi", headers=AUTH)
    assert response.status_code == 200
    assert not (repo / "channels" / "lofi").exists()


def test_create_a_channel(api, repo: Path) -> None:
    client, _state = api
    response = client.post(
        "/api/channels",
        headers=AUTH,
        json={"name": "rain", "genre": "rain", "stream_key": "aaaa-bbbb-cccc-dddd-eeee"},
    )
    assert response.status_code == 201, response.text
    assert "aaaa-bbbb" not in response.text  # the key is never echoed back

    directory = repo / "channels" / "rain"
    assert (directory / "config.yaml").is_file()
    env = directory / ".env"
    assert env.is_file()
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert "CHANNEL_MOUNT=/rain" in env.read_text(encoding="utf-8")


def test_creating_a_duplicate_channel_is_a_409(api) -> None:
    client, _state = api
    response = client.post("/api/channels", headers=AUTH, json={"name": "lofi"})
    assert response.status_code == 409
    assert response.json()["error"] == "channel_exists"


def test_creating_a_channel_with_a_hostile_name_is_a_400(api) -> None:
    client, _state = api
    response = client.post("/api/channels", headers=AUTH, json={"name": "../../etc"})
    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_channel_name", "invalid_request"}


def test_patch_updates_only_live_editable_fields(api, repo: Path) -> None:
    client, _state = api
    ok = client.patch("/api/channels/lofi", headers=AUTH, json={"genre": "sleep"})
    assert ok.status_code == 200
    config = yaml.safe_load((repo / "channels" / "lofi" / "config.yaml").read_text("utf-8"))
    assert config["genre"] == "sleep"

    # Resolution lives in .env because changing it needs a new filtergraph.
    rejected = client.patch("/api/channels/lofi", headers=AUTH, json={"resolution": "1080p"})
    assert rejected.status_code == 400


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------


def test_media_listings(api) -> None:
    client, _state = api
    audio = client.get("/api/media/audio", headers=AUTH).json()
    assert "common" in audio and "lofi" in audio["channels"]
    assert audio["channels"]["lofi"][0]["name"] == "01 - a track.m4a"

    images = client.get("/api/media/images", headers=AUTH).json()
    assert "profile" in images["channels"]["lofi"][0]


def test_put_playlist_replaces_the_whole_ordered_list(api, repo: Path) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/playlist",
        headers=AUTH,
        json={"tracks": ["channels/lofi/audio/01 - a track.m4a"], "shuffle": True},
    )
    assert response.status_code == 200
    playlist = (repo / "channels" / "lofi" / "playlist.m3u").read_text(encoding="utf-8")
    assert playlist.strip() == "/media/channel/audio/01 - a track.m4a"


@pytest.mark.parametrize(
    "entry",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "~/secrets.mp3",
        "channels/lofi/../../.env",
    ],
)
def test_a_path_outside_the_two_trees_is_rejected(api, entry: str) -> None:
    client, _state = api
    response = client.put("/api/channels/lofi/playlist", headers=AUTH, json={"tracks": [entry]})
    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_media_path", "invalid_config"}


def test_put_images_rewrites_the_generated_list(api, repo: Path) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/images",
        headers=AUTH,
        json={"slides": ["channels/lofi/images/*"], "order": "sequential"},
    )
    assert response.status_code == 200
    assert (repo / "channels" / "lofi" / "images.list").read_text(
        encoding="utf-8"
    ).strip() == "/media/channel/images/slide.jpeg"


# --------------------------------------------------------------------------
# Plugins, presets and colour
# --------------------------------------------------------------------------


def test_plugins_and_presets_are_listed(api) -> None:
    client, _state = api
    assert "plugins" in client.get("/api/plugins", headers=AUTH).json()
    presets = client.get("/api/presets", headers=AUTH).json()["presets"]
    assert [p["name"] for p in presets] == ["calm-ocean"]


def test_switching_outside_the_hot_set_is_a_409(api) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/visualisation", headers=AUTH, json={"active": "showwaves-classic"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "not_in_hot_set"


def test_switching_inside_the_hot_set_is_accepted(api) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/visualisation", headers=AUTH, json={"active": "showfreqs-bars"}
    )
    assert response.status_code == 200
    assert response.json()["active"] == "showfreqs-bars"


def test_applying_a_preset_returns_202(api, repo: Path) -> None:
    client, _state = api
    response = client.post("/api/channels/lofi/preset", headers=AUTH, json={"preset": "calm-ocean"})
    assert response.status_code == 202
    assert response.json()["preset"] == "calm-ocean"
    config = yaml.safe_load((repo / "channels" / "lofi" / "config.yaml").read_text("utf-8"))
    assert config["preset"] == "calm-ocean"
    assert config["images"]["hold_seconds"] == 30.0


def test_an_unknown_preset_is_a_400(api) -> None:
    client, _state = api
    response = client.post("/api/channels/lofi/preset", headers=AUTH, json={"preset": "nope"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_preset"


def test_a_preset_name_cannot_traverse(api) -> None:
    client, _state = api
    response = client.post(
        "/api/channels/lofi/preset", headers=AUTH, json={"preset": "../../../etc/passwd"}
    )
    assert response.status_code == 400


def test_setting_colour_persists_and_emits_validated_commands(api, repo: Path) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/colour",
        headers=AUTH,
        json={"mode": "manual", "manual": {"accent": "#4FC3F7", "tint": "#0B2A3A"}},
    )
    assert response.status_code == 200
    for message in response.json()["commands"]:
        assert len(message.split()) == 3
    config = yaml.safe_load((repo / "channels" / "lofi" / "config.yaml").read_text("utf-8"))
    assert config["colour"]["manual"]["accent"] == "#4FC3F7"


def test_a_non_hex_colour_is_a_400(api) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/colour", headers=AUTH, json={"manual": {"accent": "red", "tint": "#000000"}}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


# --------------------------------------------------------------------------
# Bumpers
# --------------------------------------------------------------------------


def test_bumpers_round_trip(api, repo: Path) -> None:
    client, _state = api
    response = client.put(
        "/api/channels/lofi/bumpers",
        headers=AUTH,
        json={
            "mode": "tracks",
            "every_tracks": 3,
            "text": {
                "version": 1,
                "voice": "af_heart",
                "bumpers": [{"id": "station-id", "text": "You're listening to lo-fi beats."}],
            },
        },
    )
    assert response.status_code == 200
    body = client.get("/api/channels/lofi/bumpers", headers=AUTH).json()
    assert body["insertion"]["every_tracks"] == 3
    assert body["text"]["bumpers"][0]["id"] == "station-id"
    assert (repo / "channels" / "lofi" / "bumpers.yaml").is_file()


def test_generating_without_text_is_a_400(api) -> None:
    client, _state = api
    response = client.post("/api/channels/lofi/bumpers/generate", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"] == "no_bumper_text"


def test_a_bumper_preview_that_does_not_exist_is_a_404(api) -> None:
    client, _state = api
    assert client.get("/api/channels/lofi/bumpers/station-id/preview", headers=AUTH).status_code == 404


def test_a_hostile_bumper_id_is_rejected(api) -> None:
    client, _state = api
    response = client.get("/api/channels/lofi/bumpers/..%2F..%2F.env/preview", headers=AUTH)
    assert response.status_code in (400, 404)


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


def test_capacity_reports_projection_against_available_cores(api) -> None:
    client, _state = api
    body = client.get("/api/capacity", headers=AUTH).json()
    assert body["available_cores"] == body["cores"] - body["reserved_cores"]
    assert [c["channel"] for c in body["channels"]] == ["lofi"]


def test_logs_validate_the_service_name(api) -> None:
    client, _state = api
    ok = client.get("/api/logs", params={"channel": "lofi", "service": "compositor"}, headers=AUTH)
    assert ok.status_code == 200
    bad = client.get(
        "/api/logs", params={"channel": "lofi", "service": "../../etc/passwd"}, headers=AUTH
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_log_service"


def test_logs_reject_an_unknown_channel(api) -> None:
    client, _state = api
    assert client.get("/api/logs", params={"channel": "nope"}, headers=AUTH).status_code == 404


def test_metrics_is_prometheus_text(api) -> None:
    client, state = api
    response = client.get("/api/metrics", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "ambient_sse_subscribers" in response.text


def test_a_published_event_reaches_a_connected_client(repo: Path, monkeypatch) -> None:
    """Driven through the real endpoint so the SSE headers and framing are covered.

    Not through TestClient: an open `text/event-stream` has no end, and the
    test transport has nothing to disconnect it with.
    """
    from ambient.api.system import events as events_endpoint
    from ambient.events import CHANNEL_STATUS
    from ambient.main import build_state

    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    state = build_state(repo)

    class StubRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> tuple[dict, list[str]]:
        response = await events_endpoint(StubRequest(), state)
        frames = response.body_iterator
        opening = await anext(frames)
        await state.events.publish(
            CHANNEL_STATUS, {"state": "running", "health": "healthy", "speed": 1.0}, channel="lofi"
        )
        event = await anext(frames)
        await frames.aclose()
        return dict(response.headers), [opening, event]

    headers, (opening, event) = asyncio.run(scenario())
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"].startswith("no-cache")
    assert headers["x-accel-buffering"] == "no"
    assert opening.startswith(":")
    assert event.startswith("event: channel.status\ndata: ")
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["channel"] == "lofi"
    assert payload["state"] == "running"
    assert payload["at"].endswith("Z")


def test_an_error_always_has_the_contract_shape(api) -> None:
    client, _state = api
    for response in (
        client.get("/api/channels"),
        client.get("/api/channels/nope", headers=AUTH),
        client.post("/api/channels/lofi/preset", headers=AUTH, json={}),
    ):
        body = response.json()
        assert set(body) >= {"error", "detail"}
        assert isinstance(body["error"], str) and " " not in body["error"]
