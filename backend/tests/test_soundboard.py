"""Soundboard effects are validated files mixed through a dedicated queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from ambient import liqctl
from ambient.media import MediaKind, MediaRoots, discover_soundboard
from tests.conftest import AUTH, RUNNING_STATE

COMMON = "/media/common/soundboard/air-horn.wav"
LOCAL = "/media/channel/soundboard/metal-pipe.wav"


def add_clips(repo: Path) -> None:
    (repo / "common" / "soundboard").mkdir(parents=True, exist_ok=True)
    (repo / "channels" / "lofi" / "soundboard").mkdir(parents=True, exist_ok=True)
    (repo / "channels" / "other" / "soundboard").mkdir(parents=True, exist_ok=True)
    (repo / "common" / "soundboard" / "air-horn.wav").write_bytes(b"shared")
    (repo / "channels" / "lofi" / "soundboard" / "metal-pipe.wav").write_bytes(b"local")
    (repo / "channels" / "other" / "soundboard" / "not-mine.wav").write_bytes(b"other")


def test_soundboard_kind_uses_audio_formats_and_its_own_folder() -> None:
    assert MediaKind.SOUNDBOARD.folder == "soundboard"
    assert ".wav" in MediaKind.SOUNDBOARD.extensions
    assert ".png" not in MediaKind.SOUNDBOARD.extensions


def test_soundboard_client_targets_only_its_queue(monkeypatch) -> None:
    sent: list[str] = []

    def fake_exchange(_host: str, line: str, **_kwargs) -> str:
        sent.append(line)
        return "21"

    monkeypatch.setattr(liqctl, "_exchange", fake_exchange)
    assert liqctl.push_soundboard("lofi-liquidsoap", COMMON, allowed=[COMMON]) == "21"
    assert sent == [f"soundboard.push {COMMON}"]
    assert liqctl.validate_command(liqctl.SOUNDBOARD_STOP) == "soundboard.flush_and_skip"

    with pytest.raises(liqctl.LiquidsoapError):
        liqctl.push_soundboard(
            "lofi-liquidsoap",
            f"{COMMON}\nshutdown",
            allowed=[f"{COMMON}\nshutdown"],
        )


def test_discovery_includes_shared_and_this_channel_only(repo: Path) -> None:
    add_clips(repo)
    roots = MediaRoots.create(repo, repo / "common", repo / "channels" / "lofi")
    result = discover_soundboard(roots)

    assert result.container_paths == [COMMON, LOCAL]
    assert [item.origin for item in result.files] == ["common", "channel"]


def test_soundboard_listing_returns_validated_container_paths(api, repo: Path) -> None:
    client, _state = api
    add_clips(repo)
    response = client.get("/api/channels/lofi/soundboard", headers=AUTH)

    assert response.status_code == 200
    clips = response.json()["clips"]
    assert [clip["container_path"] for clip in clips] == [COMMON, LOCAL]
    assert [clip["origin"] for clip in clips] == ["common", "channel"]


def test_preview_returns_a_listed_clip_without_requiring_a_running_channel(
    api, repo: Path
) -> None:
    client, _state = api
    add_clips(repo)
    response = client.get(
        "/api/channels/lofi/soundboard/preview",
        params={"clip": LOCAL},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    assert response.content == b"local"


def test_preview_refuses_a_path_outside_the_soundboard(api, repo: Path) -> None:
    client, _state = api
    add_clips(repo)
    response = client.get(
        "/api/channels/lofi/soundboard/preview",
        params={"clip": "/etc/passwd"},
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_sound"


def test_play_pushes_only_a_listed_clip_to_the_soundboard_queue(
    api, repo: Path, docker, monkeypatch
) -> None:
    client, _state = api
    add_clips(repo)
    docker.states["lofi-liquidsoap"] = RUNNING_STATE
    sent: list[tuple[str, str, tuple[str, ...]]] = []

    def fake_push(host: str, uri: str, *, allowed, **_kwargs) -> str:
        sent.append((host, uri, tuple(allowed)))
        return "17"

    monkeypatch.setattr(liqctl, "push_soundboard", fake_push)
    response = client.post(
        "/api/channels/lofi/soundboard/play",
        json={"clip": LOCAL},
        headers=AUTH,
    )

    assert response.status_code == 202
    assert response.json()["action"] == "soundboard_play"
    assert sent == [("lofi-liquidsoap", LOCAL, (COMMON, LOCAL))]


def test_unknown_sound_is_refused(api, repo: Path, docker) -> None:
    client, _state = api
    add_clips(repo)
    docker.states["lofi-liquidsoap"] = RUNNING_STATE
    response = client.post(
        "/api/channels/lofi/soundboard/play",
        json={"clip": "/etc/passwd"},
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_sound"


def test_soundboard_stop_flushes_and_skips(api, docker, monkeypatch) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE
    sent: list[tuple[str, str]] = []

    def fake_send(host: str, command: str, **_kwargs) -> str:
        sent.append((host, command))
        return "Done."

    monkeypatch.setattr(liqctl, "send", fake_send)
    response = client.post("/api/channels/lofi/soundboard/stop", headers=AUTH)

    assert response.status_code == 202
    assert sent == [("lofi-liquidsoap", "soundboard.flush_and_skip")]


@pytest.mark.parametrize("path", ["play", "stop"])
def test_soundboard_controls_require_a_running_channel(api, path: str) -> None:
    client, _state = api
    kwargs = {"json": {"clip": COMMON}} if path == "play" else {}
    response = client.post(f"/api/channels/lofi/soundboard/{path}", headers=AUTH, **kwargs)
    assert response.status_code == 409
    assert response.json()["error"] == "channel_not_running"


def test_soundboard_requires_a_token(api) -> None:
    client, _state = api
    assert client.get("/api/channels/lofi/soundboard").status_code == 401
    assert client.get(
        "/api/channels/lofi/soundboard/preview", params={"clip": COMMON}
    ).status_code == 401
    assert client.post("/api/channels/lofi/soundboard/stop").status_code == 401


def test_liquidsoap_graph_mixes_a_dedicated_effect_queue() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "liquidsoap" / "channel.liq").read_text(encoding="utf-8")
    assert 'request.queue(id="soundboard")' in script
    assert "limit(smooth_add(" in script
    assert "normal=faded, special=effects" in script