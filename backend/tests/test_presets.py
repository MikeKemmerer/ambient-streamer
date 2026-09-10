"""Presets: what they may set, and the zmq messages applying one produces."""

from __future__ import annotations

from pathlib import Path

import pytest

from ambient.config import load_channel, load_workspace
from ambient.models import ColorMode
from ambient.presets import (
    NotInHotSet,
    Preset,
    PresetError,
    apply_preset,
    color_messages,
    color_targets,
    effect_messages,
    get_preset,
    load_registry,
    nearest_rotation,
)
from ambient.zmqctl import ZmqValidationError, validate_message


def channel_config(repo: Path):
    workspace = load_workspace(repo)
    return load_channel(workspace, "lofi", resolve_media=False).config


# --------------------------------------------------------------------------
# What a preset may not set
# --------------------------------------------------------------------------


def test_a_preset_cannot_touch_hot_set() -> None:
    with pytest.raises(Exception):
        Preset.model_validate(
            {"name": "x", "visualization": {"active": "a", "hot_set": ["a", "b"]}}
        )


def test_a_preset_cannot_touch_resolution_fps_or_encoder() -> None:
    for field in ("resolution", "fps", "encoder"):
        with pytest.raises(Exception):
            Preset.model_validate({"name": "x", field: "720p"})


def test_a_preset_cannot_select_media() -> None:
    with pytest.raises(Exception):
        Preset.model_validate({"name": "x", "audio": {"tracks": ["common/audio/a.mp3"]}})


def test_an_unknown_preset_name_is_rejected_without_touching_the_filesystem(repo: Path) -> None:
    with pytest.raises(PresetError, match="invalid preset name"):
        get_preset(repo / "presets", "../../etc/passwd")


def test_the_registry_skips_a_broken_preset(repo: Path) -> None:
    (repo / "presets" / "broken.yaml").write_text("visualization: [nope]\n", encoding="utf-8")
    registry = load_registry(repo / "presets")
    assert "calm-ocean" in registry
    assert "broken" not in registry


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


def test_applying_merges_over_the_channel_config(repo: Path) -> None:
    preset = get_preset(repo / "presets", "calm-ocean")
    application = apply_preset(preset, channel_config(repo))

    assert application.config.preset == "calm-ocean"
    assert application.config.images.hold_seconds == 30.0
    assert application.config.audio.crossfade_seconds == 6.0
    assert application.config.color.mode is ColorMode.MANUAL
    assert application.config.color.manual.accent == "#4FC3F7"
    # Untouched fields survive.
    assert application.config.visualization.hot_set == ["showfreqs-bars"]
    assert application.rewrite_images_list is True
    assert application.reconfigure_liquidsoap is True


def test_a_visualization_outside_the_hot_set_is_rejected(repo: Path) -> None:
    body = (repo / "presets" / "calm-ocean.yaml").read_text(encoding="utf-8")
    (repo / "presets" / "neon.yaml").write_text(
        body.replace("name: calm-ocean", "name: neon").replace(
            "active: showfreqs-bars", "active: showwaves-classic"
        ),
        encoding="utf-8",
    )
    preset = get_preset(repo / "presets", "neon")
    with pytest.raises(NotInHotSet, match="hot_set"):
        apply_preset(preset, channel_config(repo))


def test_applying_the_same_preset_twice_is_idempotent(repo: Path) -> None:
    preset = get_preset(repo / "presets", "calm-ocean")
    once = apply_preset(preset, channel_config(repo))
    twice = apply_preset(preset, once.config)
    assert once.config.model_dump() == twice.config.model_dump()


# --------------------------------------------------------------------------
# Color
# --------------------------------------------------------------------------


def test_every_generated_message_survives_the_validator() -> None:
    messages = color_messages("#4FC3F7", "#0B2A3A", transition_seconds=4.0, stream_time=120.5)
    assert len(messages) == 3
    for message in messages:
        target, command, value = validate_message(message)
        assert "@" in target
        assert len(message.split()) == 3


def test_a_transition_is_one_self_animating_expression_per_filter() -> None:
    messages = color_messages("#FF8800", "#101820", transition_seconds=4.0, stream_time=10.0)
    ramps = [m for m in messages if "min(max((t-10)" in m]
    assert len(ramps) == 3  # one message each, not a command stream


def test_a_transition_starts_from_the_current_color() -> None:
    current = color_targets("#4FC3F7", "#0B2A3A")
    messages = color_messages(
        "#FF8800",
        "#101820",
        transition_seconds=4.0,
        stream_time=10.0,
        current=current,
        current_accent="#4FC3F7",
        baked_accent="#4FC3F7",
    )

    assert any(f"saturation {current.saturation:g}+(" in message for message in messages)
    assert any(f"brightness {current.brightness:g}+(" in message for message in messages)
    assert any("hue@viz h 0+(" in message for message in messages)


def test_a_transition_without_stream_time_uses_final_values() -> None:
    messages = color_messages("#FF8800", "#101820", transition_seconds=4.0)
    assert all("t-" not in message for message in messages)


def test_hue_rotation_takes_the_short_path_across_the_boundary() -> None:
    assert nearest_rotation(179.0, -179.0) == 181.0
    assert nearest_rotation(-179.0, 179.0) == -181.0


def test_a_zero_second_transition_sends_a_plain_value() -> None:
    messages = color_messages("#FF8800", "#101820", transition_seconds=0.0)
    assert all("t-" not in message for message in messages)


def test_effect_values_are_range_checked_client_side() -> None:
    preset = Preset.model_validate(
        {"name": "x", "effects": {"eq": {"brightness": -0.05, "saturation": 0.9}, "hue": {"h": 190}}}
    )
    messages = effect_messages(preset.effects)
    assert "eq@eq brightness -0.05" in messages
    assert "hue@hue h 190" in messages
    with pytest.raises(Exception):
        Preset.model_validate({"name": "x", "effects": {"eq": {"brightness": 99}}})


def test_an_out_of_range_value_never_reaches_ffmpeg() -> None:
    # FFmpeg answers `0 Success` and ignores it, so the check has to be here.
    with pytest.raises(ZmqValidationError):
        from ambient.zmqctl import build_message

        build_message("eq@eq", "brightness", "99")


def test_color_targets_stay_inside_the_filters_real_ranges() -> None:
    for accent, tint in (("#FFFFFF", "#FFFFFF"), ("#000000", "#000000"), ("#FF0000", "#00FF00")):
        targets = color_targets(accent, tint)
        assert -360.0 <= targets.hue_degrees <= 360.0
        assert 0.0 <= targets.saturation <= 3.0
        assert -1.0 <= targets.brightness <= 1.0
