"""Internal channels: rendered and served on the network, never sent to YouTube.

Not "leave the stream key blank". The composer publishes only the local
rendition, so the relay path that carries the YouTube publisher hook never goes
ready — mediamtx.yml hangs `runOnReady` on the program path alone. A key pasted
into the wrong channel therefore cannot put an internal channel on air.

It is also cheaper than a public channel, not more expensive: one encode
instead of two, because there is no second rendition to preview.
"""

from __future__ import annotations

from pathlib import Path

from ambient.config import Workspace, load_channel, load_workspace
from ambient.supervisor import compose_context, render_compose
from tests.conftest import AUTH, RUNNING_STATE
from tests.test_config import make_repo


def _set_env(directory: Path, **values: str) -> None:
    path = directory / ".env"
    kept = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0] not in values
    ]
    kept += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def resolve(root: Path, **env: str):
    if env:
        _set_env(root / "channels" / "lofi", **env)
    workspace: Workspace = load_workspace(root)
    return load_channel(workspace, "lofi"), workspace


def test_a_channel_publishes_to_youtube_by_default(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path))
    assert channel.publish_youtube is True
    assert channel.local_only is False


def test_the_local_rendition_defaults_to_an_operator_preview(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path))
    assert (channel.local_width, channel.local_height, channel.local_fps) == (640, 360, 15)


def test_an_internal_channel_serves_its_own_full_size(tmp_path: Path) -> None:
    """There is no second rendition, so this one is the stream."""
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_PUBLISH_YOUTUBE="false")
    assert channel.local_only is True
    assert (channel.local_width, channel.local_height) == (channel.width, channel.height)
    assert channel.local_fps == channel.fps


def test_an_internal_channel_does_not_warn_about_a_missing_stream_key(tmp_path: Path) -> None:
    channel, _ = resolve(
        make_repo(tmp_path), CHANNEL_PUBLISH_YOUTUBE="false", YOUTUBE_STREAM_KEY=""
    )
    assert not [w for w in channel.warnings if "STREAM_KEY" in w]


def test_a_public_channel_still_warns_about_a_missing_stream_key(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path), YOUTUBE_STREAM_KEY="")
    assert [w for w in channel.warnings if "STREAM_KEY" in w]


def test_the_local_height_is_overridable_and_the_width_follows(tmp_path: Path) -> None:
    channel, _ = resolve(
        make_repo(tmp_path), CHANNEL_LOCAL_HEIGHT="540", CHANNEL_LOCAL_FPS="24"
    )
    assert (channel.local_width, channel.local_height, channel.local_fps) == (960, 540, 24)


def test_an_odd_width_is_rounded_up(tmp_path: Path) -> None:
    """yuv420p cannot encode an odd dimension."""
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_LOCAL_HEIGHT="145")
    assert channel.local_width % 2 == 0


def test_the_composer_is_told_which_legs_to_publish(tmp_path: Path) -> None:
    channel, workspace = resolve(make_repo(tmp_path))
    context = compose_context(workspace, channel)
    assert context["publish_program"] == "on"
    assert context["preview_height"] == "360"


def test_an_internal_channel_renders_with_the_program_leg_off(tmp_path: Path) -> None:
    channel, workspace = resolve(make_repo(tmp_path), CHANNEL_PUBLISH_YOUTUBE="false")
    rendered = render_compose(workspace, channel)
    assert 'PUBLISH_PROGRAM: "off"' in rendered
    assert f'PREVIEW_HEIGHT: "{channel.height}"' in rendered


def test_status_reports_no_youtube_leg_rather_than_a_fault(api) -> None:
    """`rtmp: disconnected` on an internal channel would read as an outage."""
    client, state = api
    _set_env(state.workspace.root / "channels" / "lofi", CHANNEL_PUBLISH_YOUTUBE="false")

    body = client.get("/api/channels/lofi", headers=AUTH).json()
    assert body["youtube"] is False
    assert body["rtmp"] == "local-only"


def test_delivery_can_turn_youtube_off(api) -> None:
    client, state = api
    response = client.put(
        "/api/channels/lofi/delivery", json={"youtube": False}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["youtube"] is False
    assert load_channel(state.workspace, "lofi").local_only is True


def test_turning_youtube_back_on_restores_the_preview_size(api) -> None:
    client, _state = api
    client.put("/api/channels/lofi/delivery", json={"youtube": False}, headers=AUTH)
    response = client.put(
        "/api/channels/lofi/delivery", json={"youtube": True}, headers=AUTH
    )
    assert response.json()["local"] == "640x360@15"


def test_delivery_is_refused_while_the_channel_runs(api, docker) -> None:
    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    response = client.put(
        "/api/channels/lofi/delivery", json={"youtube": False}, headers=AUTH
    )
    assert response.status_code == 409
    assert response.json()["error"] == "channel_running"
