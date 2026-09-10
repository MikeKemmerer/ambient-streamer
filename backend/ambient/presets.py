"""Presets: a named bundle of look-and-feel settings.

A preset must never require a restart, so the model simply has no field for
anything that would: not `hot_set`, not resolution/fps/encoder, not media
selection. What it can reach is what the escape hatches reach — zmq commands,
`streamselect`, and the generated lists.

Color is emitted as a self-animating `eq`/`hue` expression rather than a
command stream. Measured: the zmq filter ceilings at ~31.5 commands/s because
it only polls when a frame passes, so a per-frame ramp is impossible; one
expression moves the value every frame with no further traffic.
"""

from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from .models import (
    HEX_COLOR,
    ChannelConfig,
    Color,
    ColorMode,
    ImageOrder,
    ManualColor,
    StrictModel,
)
from .zmqctl import ZmqValidationError, build_message

LOG = logging.getLogger("ambient.presets")

EQ_TARGET = "eq@eq"
HUE_TARGET = "hue@hue"
# The visualization's own grade, upstream of the blend. The accent is baked into
# the plugins at launch, so this can only ever be a rotation away from that.
VIZ_TARGET = "hue@viz"

# `eq` brightness is -1..1 but anything past a fifth of that is unwatchable.
BRIGHTNESS_RANGE = 0.2


class PresetError(ValueError):
    """The preset is malformed, unknown, or not applicable to this channel."""


class NotInHotSet(PresetError):
    """The preset's visualization is not instantiated on this channel."""


class PresetVisualization(StrictModel):
    # No hot_set: changing it means a new filtergraph, which means a restart.
    active: str


class PresetSlideshow(StrictModel):
    hold_seconds: float | None = Field(None, gt=0)
    fade_seconds: float | None = Field(None, ge=0)
    order: ImageOrder | None = None


class PresetAudio(StrictModel):
    crossfade_seconds: float | None = Field(None, ge=0)


class PresetEq(StrictModel):
    brightness: float | None = Field(None, ge=-1.0, le=1.0)
    saturation: float | None = Field(None, ge=0.0, le=3.0)
    contrast: float | None = Field(None, ge=-1000.0, le=1000.0)
    gamma: float | None = Field(None, ge=0.1, le=10.0)


class PresetHue(StrictModel):
    h: float | None = Field(None, ge=-360.0, le=360.0)
    s: float | None = Field(None, ge=-10.0, le=10.0)
    b: float | None = Field(None, ge=-10.0, le=10.0)


class PresetEffects(StrictModel):
    eq: PresetEq = Field(default_factory=PresetEq)
    hue: PresetHue = Field(default_factory=PresetHue)


class PresetColor(StrictModel):
    mode: ColorMode | None = None
    manual: ManualColor | None = None
    transition_seconds: float | None = Field(None, ge=0)


class Preset(StrictModel):
    version: Literal[1] = 1
    name: str
    display_name: str = ""
    description: str = ""
    visualization: PresetVisualization | None = None
    color: PresetColor | None = None
    slideshow: PresetSlideshow = Field(default_factory=PresetSlideshow)
    audio: PresetAudio = Field(default_factory=PresetAudio)
    effects: PresetEffects = Field(default_factory=PresetEffects)

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if not value or "/" in value or value.startswith("."):
            raise ValueError(f"preset name {value!r} must be a plain file stem")
        return value


def load_preset(path: Path) -> Preset:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PresetError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PresetError(f"{path}: expected a mapping at the top level")
    raw.setdefault("name", path.stem)
    try:
        return Preset.model_validate(raw)
    except ValidationError as exc:
        raise PresetError(f"{path}: {exc.errors()[0]['msg']}") from exc


def load_registry(presets_dir: Path) -> dict[str, Preset]:
    registry: dict[str, Preset] = {}
    if not Path(presets_dir).is_dir():
        return registry
    for path in sorted(Path(presets_dir).glob("*.yaml")):
        try:
            preset = load_preset(path)
        except PresetError as exc:
            LOG.warning("skipping preset %s: %s", path.name, exc)
            continue
        registry[preset.name] = preset
    return registry


def get_preset(presets_dir: Path, name: str) -> Preset:
    """Name comes from a request: resolve it as a stem, never as a path."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PresetError(f"invalid preset name {name!r}")
    path = Path(presets_dir) / f"{name}.yaml"
    if not path.is_file():
        raise PresetError(f"preset {name!r} does not exist")
    return load_preset(path)


# --------------------------------------------------------------------------
# Color
# --------------------------------------------------------------------------


def parse_hex(color: str) -> tuple[float, float, float]:
    text = color.strip().lstrip("#")
    if len(text) != 6:
        raise PresetError(f"{color!r} is not a #rrggbb color")
    try:
        return tuple(int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise PresetError(f"{color!r} is not a #rrggbb color") from exc


@dataclass(frozen=True)
class ColorTargets:
    """The `eq`/`hue` values a color pair maps to."""

    hue_degrees: float
    saturation: float
    brightness: float


# What an untouched filtergraph renders at, and where a ramp starts from.
NEUTRAL_TARGETS = ColorTargets(hue_degrees=0.0, saturation=1.0, brightness=0.0)


def color_targets(accent: str, tint: str) -> ColorTargets:
    red, green, blue = parse_hex(accent)
    hue, _lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    tint_luma = sum(c * w for c, w in zip(parse_hex(tint), (0.2126, 0.7152, 0.0722)))
    return ColorTargets(
        hue_degrees=round(hue * 360.0 - 180.0, 2),
        saturation=round(0.8 + saturation * 0.8, 3),
        brightness=round((tint_luma - 0.5) * 2 * BRIGHTNESS_RANGE, 3),
    )


def _ramp(start: float, end: float, seconds: float, at: float) -> str:
    """One self-animating expression: `t` is stream time, `at` is now."""
    if seconds <= 0 or abs(end - start) < 1e-6:
        return f"{end:g}"
    return f"{start:g}+({end - start:g})*min(max((t-{at:g})/{seconds:g},0),1)"


def hue_degrees(color: str) -> float:
    """True hue of a hex color, 0-360."""
    red, green, blue = parse_hex(color)
    hue, _lightness, _saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360.0


def viz_rotation(baked: str, wanted: str) -> float:
    """Shortest rotation from the baked accent to the requested one.

    `hue` rotates, it does not set, so an absolute target is only meaningful
    relative to the color the plugins were built with.
    """
    delta = hue_degrees(wanted) - hue_degrees(baked)
    return round((delta + 180.0) % 360.0 - 180.0, 2)


def color_messages(
    accent: str,
    tint: str,
    *,
    transition_seconds: float = 0.0,
    stream_time: float = 0.0,
    current: ColorTargets | None = None,
    baked_accent: str = "",
) -> list[str]:
    """Validated `TARGET COMMAND ARG` messages for a color change.

    Every message goes through `zmqctl.build_message`: a malformed one aborts
    FFmpeg with SIGABRT.
    """
    target = color_targets(accent, tint)
    start = current or NEUTRAL_TARGETS
    pairs = [
        (EQ_TARGET, "saturation", start.saturation, target.saturation),
        (EQ_TARGET, "brightness", start.brightness, target.brightness),
    ]
    if baked_accent:
        # The accent belongs to the visualization. Rotating the whole composite
        # by it as well would turn those bars straight back off-target: measured
        # green -> red on hue@viz, then hue@hue rotated the result to cyan.
        pairs.append((VIZ_TARGET, "h", 0.0, viz_rotation(baked_accent, accent)))
    else:
        pairs.insert(0, (HUE_TARGET, "h", start.hue_degrees, target.hue_degrees))
    messages: list[str] = []
    for filter_target, param, begin, end in pairs:
        value = _ramp(begin, end, transition_seconds, stream_time)
        try:
            messages.append(build_message(filter_target, param, value))
        except ZmqValidationError as exc:
            raise PresetError(f"{filter_target} {param}: {exc}") from exc
    return messages


def effect_messages(effects: PresetEffects) -> list[str]:
    messages: list[str] = []
    for param, value in effects.eq.model_dump(exclude_none=True).items():
        messages.append(build_message(EQ_TARGET, param, f"{value:g}"))
    for param, value in effects.hue.model_dump(exclude_none=True).items():
        messages.append(build_message(HUE_TARGET, param, f"{value:g}"))
    return messages


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@dataclass
class PresetApplication:
    """What applying a preset changed, and what has to happen next."""

    preset: str
    config: ChannelConfig
    messages: list[str] = field(default_factory=list)
    visualization: str | None = None
    rewrite_images_list: bool = False
    reconfigure_liquidsoap: bool = False
    changed: list[str] = field(default_factory=list)


def apply_preset(
    preset: Preset, config: ChannelConfig, *, stream_time: float = 0.0
) -> PresetApplication:
    """Merge a preset over a channel config. The preset wins where it sets."""
    data = config.model_dump()
    application = PresetApplication(preset=preset.name, config=config)

    if preset.visualization is not None:
        active = preset.visualization.active
        if active not in config.visualization.hot_set:
            raise NotInHotSet(
                f"preset {preset.name!r} wants {active!r}, which is not in this "
                f"channel's hot_set; promoting it would need a restart"
            )
        if active != config.visualization.active:
            data["visualization"]["active"] = active
            application.visualization = active
            application.changed.append("visualization.active")

    if preset.slideshow.hold_seconds is not None:
        data["images"]["hold_seconds"] = preset.slideshow.hold_seconds
        application.rewrite_images_list = True
        application.changed.append("images.hold_seconds")
    if preset.slideshow.fade_seconds is not None:
        data["images"]["fade_seconds"] = preset.slideshow.fade_seconds
        application.rewrite_images_list = True
        application.changed.append("images.fade_seconds")
    if preset.slideshow.order is not None:
        data["images"]["order"] = preset.slideshow.order
        application.rewrite_images_list = True
        application.changed.append("images.order")

    if preset.audio.crossfade_seconds is not None:
        data["audio"]["crossfade_seconds"] = preset.audio.crossfade_seconds
        application.reconfigure_liquidsoap = True
        application.changed.append("audio.crossfade_seconds")

    color = Color.model_validate(data["color"])
    if preset.color is not None:
        merged = color.model_dump()
        if preset.color.mode is not None:
            merged["mode"] = preset.color.mode
        if preset.color.manual is not None:
            merged["manual"] = preset.color.manual.model_dump()
        if preset.color.transition_seconds is not None:
            merged["transition_seconds"] = preset.color.transition_seconds
        color = Color.model_validate(merged)
        data["color"] = color.model_dump()
        application.changed.append("color")

    if color.mode is ColorMode.MANUAL:
        application.messages.extend(
            color_messages(
                color.manual.accent,
                color.manual.tint,
                transition_seconds=color.transition_seconds,
                stream_time=stream_time,
            )
        )
    application.messages.extend(effect_messages(preset.effects))

    data["preset"] = preset.name
    application.config = ChannelConfig.model_validate(data)
    return application


__all__ = [
    "HEX_COLOR",
    "ColorTargets",
    "NotInHotSet",
    "Preset",
    "PresetApplication",
    "PresetError",
    "apply_preset",
    "color_messages",
    "color_targets",
    "effect_messages",
    "get_preset",
    "load_preset",
    "load_registry",
]
