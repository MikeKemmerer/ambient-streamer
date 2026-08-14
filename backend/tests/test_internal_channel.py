"""Delivery targets: YouTube, internal video, internal audio — any combination.

Each target the composer is not told to publish is a relay path that never goes
ready. `docker/mediamtx.yml` hangs the YouTube publisher hook on the program
path alone, so a channel without the `youtube` target cannot reach YouTube even
with a valid stream key in its `.env` — that is structural, not a blank key
someone could paste into by accident.

The two internal feeds are served from the same shape as the operator preview:
`<host>:<hls>/<channel>/<preview|video|audio>/index.m3u8`.
"""

from __future__ import annotations

from pathlib import Path

from ambient.config import (
    ConfigError,
    Workspace,
    load_channel,
    load_workspace,
    parse_delivery,
    resolve_delivery,
)
from ambient.models import ChannelEnv, DeliveryTarget
from ambient.supervisor import compose_context, render_compose
from tests.conftest import AUTH, RUNNING_STATE
from tests.test_config import make_repo

import pytest


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


# --------------------------------------------------------------------------
# Parsing the target list
# --------------------------------------------------------------------------


def test_targets_parse_in_a_stable_order_however_they_were_written() -> None:
    """The rendered `.env` must not churn just because a UI reordered a set."""
    assert parse_delivery("audio,youtube") == parse_delivery("youtube, audio")
    assert parse_delivery("audio,youtube") == [DeliveryTarget.YOUTUBE, DeliveryTarget.AUDIO]


def test_duplicates_and_blanks_are_ignored() -> None:
    assert parse_delivery("video,,video, ") == [DeliveryTarget.VIDEO]


def test_an_unknown_target_is_refused_rather_than_dropped() -> None:
    """Silently ignoring it would take a channel off air without saying so."""
    with pytest.raises(ConfigError) as exc:
        parse_delivery("youtube,twitch")
    assert "twitch" in str(exc.value)


# --------------------------------------------------------------------------
# Migration off the old boolean
# --------------------------------------------------------------------------


def env(**values: str) -> ChannelEnv:
    base = {"CHANNEL_MOUNT": "/lofi", "CHANNEL_FALLBACK_MOUNT": "/lofi-fallback"}
    return ChannelEnv.model_validate({**base, **values})


def test_a_channel_with_neither_setting_publishes_to_youtube() -> None:
    assert resolve_delivery(env()) == [DeliveryTarget.YOUTUBE]


def test_a_channel_deliberately_taken_off_youtube_stays_off_it() -> None:
    """An upgrade must not put a struck or private channel back on air."""
    assert resolve_delivery(env(CHANNEL_PUBLISH_YOUTUBE="false")) == [DeliveryTarget.VIDEO]


def test_the_target_list_wins_over_the_old_boolean() -> None:
    resolved = resolve_delivery(
        env(CHANNEL_PUBLISH_YOUTUBE="false", CHANNEL_DELIVERY="youtube")
    )
    assert resolved == [DeliveryTarget.YOUTUBE]


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_a_channel_publishes_to_youtube_by_default(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path))
    assert channel.publish_youtube is True
    assert channel.local_only is False


def test_the_internal_video_feed_defaults_to_the_channels_own_size(tmp_path: Path) -> None:
    """It is a product, not a preview, so it is not shrunk."""
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_DELIVERY="video")
    assert (channel.local_width, channel.local_height) == (channel.width, channel.height)
    assert channel.local_fps == channel.fps


def test_the_internal_size_is_overridable_and_the_width_follows(tmp_path: Path) -> None:
    channel, _ = resolve(
        make_repo(tmp_path), CHANNEL_LOCAL_HEIGHT="540", CHANNEL_LOCAL_FPS="24"
    )
    assert (channel.local_width, channel.local_height, channel.local_fps) == (960, 540, 24)


def test_an_odd_width_is_rounded_up(tmp_path: Path) -> None:
    """yuv420p cannot encode an odd dimension."""
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_LOCAL_HEIGHT="145")
    assert channel.local_width % 2 == 0


def test_every_combination_resolves(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_DELIVERY="youtube,video,audio")
    assert (channel.publish_youtube, channel.publish_video, channel.publish_audio) == (
        True,
        True,
        True,
    )


def test_audio_only_is_a_valid_channel(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path), CHANNEL_DELIVERY="audio")
    assert channel.publish_audio is True
    assert channel.publish_youtube is False
    assert channel.local_only is True


def test_a_channel_off_youtube_does_not_warn_about_a_missing_stream_key(
    tmp_path: Path,
) -> None:
    channel, _ = resolve(
        make_repo(tmp_path), CHANNEL_DELIVERY="video", YOUTUBE_STREAM_KEY=""
    )
    assert not [w for w in channel.warnings if "STREAM_KEY" in w]


def test_a_youtube_channel_still_warns_about_a_missing_stream_key(tmp_path: Path) -> None:
    channel, _ = resolve(make_repo(tmp_path), YOUTUBE_STREAM_KEY="")
    assert [w for w in channel.warnings if "STREAM_KEY" in w]


# --------------------------------------------------------------------------
# What the composer is told
# --------------------------------------------------------------------------


def test_the_composer_is_told_which_legs_to_publish(tmp_path: Path) -> None:
    channel, workspace = resolve(make_repo(tmp_path))
    context = compose_context(workspace, channel)
    assert context["publish_program"] == "on"
    assert context["publish_video"] == "off"
    assert context["publish_audio"] == "off"


def test_the_operator_preview_stays_small_whatever_the_internal_feed_is(
    tmp_path: Path,
) -> None:
    """The preview is for the control plane's own player; sizing it up is waste."""
    channel, workspace = resolve(make_repo(tmp_path), CHANNEL_DELIVERY="video")
    context = compose_context(workspace, channel)
    assert (context["preview_width"], context["preview_height"], context["preview_fps"]) == (
        "640",
        "360",
        "15",
    )
    assert context["local_height"] == str(channel.height)


def test_an_internal_channel_renders_with_the_program_leg_off(tmp_path: Path) -> None:
    channel, workspace = resolve(make_repo(tmp_path), CHANNEL_DELIVERY="video,audio")
    rendered = render_compose(workspace, channel)
    assert 'PUBLISH_PROGRAM: "off"' in rendered
    assert 'PUBLISH_VIDEO: "on"' in rendered
    assert 'PUBLISH_AUDIO: "on"' in rendered
    assert f'LOCAL_HEIGHT: "{channel.height}"' in rendered


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_status_reports_no_youtube_leg_rather_than_a_fault(api) -> None:
    """`rtmp: disconnected` on an internal channel would read as an outage."""
    client, state = api
    _set_env(state.workspace.root / "channels" / "lofi", CHANNEL_DELIVERY="video")

    body = client.get("/api/channels/lofi", headers=AUTH).json()
    assert body["delivery"] == ["video"]
    assert body["youtube"] is False
    assert body["rtmp"] == "local-only"


def test_delivery_replaces_the_whole_target_set(api, state=None) -> None:
    client, state = api
    response = client.put(
        "/api/channels/lofi/delivery", json={"targets": ["video", "audio"]}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["delivery"] == ["video", "audio"]
    assert load_channel(state.workspace, "lofi").local_only is True


def test_an_empty_target_set_is_refused(api) -> None:
    """A channel that delivers nowhere is a mistake, not a configuration."""
    client, _state = api
    response = client.put("/api/channels/lofi/delivery", json={"targets": []}, headers=AUTH)
    assert response.status_code == 400
    assert "targets" in response.text


def test_setting_targets_clears_the_superseded_boolean(api) -> None:
    """Leaving both would let a stale `false` contradict an explicit list."""
    client, state = api
    _set_env(state.workspace.root / "channels" / "lofi", CHANNEL_PUBLISH_YOUTUBE="false")

    client.put("/api/channels/lofi/delivery", json={"targets": ["youtube"]}, headers=AUTH)

    assert load_channel(state.workspace, "lofi").publish_youtube is True


def test_delivery_is_refused_while_the_channel_runs(api, docker) -> None:
    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    response = client.put(
        "/api/channels/lofi/delivery", json={"targets": ["video"]}, headers=AUTH
    )
    assert response.status_code == 409
    assert response.json()["error"] == "channel_running"


# --------------------------------------------------------------------------
# The URLs, which are the point of having internal feeds at all
# --------------------------------------------------------------------------


def test_only_published_feeds_are_advertised(api, monkeypatch) -> None:
    """A URL for a feed nobody started is a support call, not a convenience."""
    client, state = api
    monkeypatch.setattr(state.workspace.env, "hls_publish", 8888)
    _set_env(state.workspace.root / "channels" / "lofi", CHANNEL_DELIVERY="youtube")

    feeds = client.get("/api/channels/lofi", headers=AUTH).json()["feeds"]

    assert [f["rendition"] for f in feeds] == ["preview"]


def test_each_internal_feed_gets_the_preview_shaped_url(api, monkeypatch) -> None:
    client, state = api
    monkeypatch.setattr(state.workspace.env, "hls_publish", 8888)
    _set_env(
        state.workspace.root / "channels" / "lofi", CHANNEL_DELIVERY="video,audio"
    )

    feeds = client.get("/api/channels/lofi", headers=AUTH).json()["feeds"]

    assert [f["rendition"] for f in feeds] == ["preview", "video", "audio"]
    assert [f["url"] for f in feeds] == [
        "http://testserver:8888/lofi/preview/index.m3u8",
        "http://testserver:8888/lofi/video/index.m3u8",
        "http://testserver:8888/lofi/audio/index.m3u8",
    ]


def test_no_url_is_offered_when_the_relay_port_is_not_published(api, monkeypatch) -> None:
    client, state = api
    monkeypatch.setattr(state.workspace.env, "hls_publish", None)
    _set_env(state.workspace.root / "channels" / "lofi", CHANNEL_DELIVERY="video")

    feeds = client.get("/api/channels/lofi", headers=AUTH).json()["feeds"]

    assert [f["rendition"] for f in feeds] == ["preview", "video"]
    assert all(f["url"] is None for f in feeds)
