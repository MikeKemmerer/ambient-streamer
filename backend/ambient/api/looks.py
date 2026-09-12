"""Plugins, presets, visualization switching and color."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import Field

from .. import colorprofile
from .. import plugins as plugin_registry
from .. import presets as preset_registry
from ..config import ResolvedChannel
from ..main import ApiError, AppState
from ..models import ChannelConfig, ColorMode, ManualColor, StrictModel
from ..watchdog import parse_progress
from ..zmqctl import ZmqCommandError, ZmqValidationError, build_message
from .deps import (
    Authed,
    VisualMutation,
    parse_now_json,
    publish_visualization_event,
    queue_visualizer_action,
    recompile,
    save_channel_config,
)

LOG = logging.getLogger("ambient.api.looks")

router = APIRouter(prefix="/api", tags=["looks"])

# Timeline `enable` on the composite, which is what makes standby one frame.
OVERLAY_TARGET = "overlay@viz"
OPACITY_TARGET = "lut@vizop"
COLOR_MODE_SETTLE_SECONDS = 0.6


class VisualizationBody(StrictModel):
    active: str


class HotSetBody(StrictModel):
    hot_set: list[str]
    active: str | None = None


class VisibleBody(StrictModel):
    visible: bool


class OpacityBody(StrictModel):
    opacity: float = Field(..., ge=0, le=1)


class ParametersBody(StrictModel):
    plugin: str
    values: dict[str, float | int | bool | str]


class PresetBody(StrictModel):
    preset: str


class ColorBody(StrictModel):
    mode: ColorMode | None = None
    manual: ManualColor | None = None
    transition_seconds: float | None = Field(None, ge=0)


@router.get("/plugins")
async def list_plugins(state: AppState = Authed) -> dict[str, Any]:
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    return {
        "plugins": [
            {
                "name": manifest.name,
                "display_name": manifest.display_name,
                "description": manifest.description,
                "version": manifest.version,
                "author": manifest.author,
                "commandable": [
                    {"target": c.target, "param": c.param, "type": c.type}
                    for c in manifest.commandable
                ],
                "cost": {
                    "cores_720p30": manifest.cores_720p30,
                    "scale_1080p": manifest.scale_1080p,
                },
                "requires_filters": list(manifest.requires_filters),
                "output_size": manifest.declared_size,
                "parameters": [
                    {
                        "name": p.name,
                        "label": p.label,
                        "description": p.description,
                        "type": p.type,
                        "min": p.minimum,
                        "max": p.maximum,
                        "step": p.step,
                        "default": p.default,
                        "choices": [{"value": v, "label": lbl} for v, lbl in p.choices],
                    }
                    for p in manifest.parameters
                ],
            }
            for manifest in registry.values()
        ]
    }


@router.get("/presets")
async def list_presets(state: AppState = Authed) -> dict[str, Any]:
    registry = preset_registry.load_registry(state.workspace.presets_dir)
    return {
        "presets": [
            {
                "name": preset.name,
                "display_name": preset.display_name or preset.name,
                "description": preset.description,
                "visualization": preset.visualization.active if preset.visualization else None,
                "color": preset.color.model_dump(mode="json") if preset.color else None,
                "slideshow": preset.slideshow.model_dump(mode="json", exclude_none=True),
                "audio": preset.audio.model_dump(mode="json", exclude_none=True),
            }
            for preset in registry.values()
        ]
    }


@router.put("/channels/{name}/visualization")
async def set_visualization(
    name: str,
    body: VisualizationBody,
    response: Response,
    allow_restart: bool = Query(
        True, deprecated=True, description="ignored; visualizer changes never restart composer"
    ),
    state: AppState = VisualMutation,
) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    changed = body.active != channel.config.visualization.active
    if changed and body.active not in registry:
        raise ApiError(
            404,
            "unknown_plugin",
            f"{body.active!r} is not installed; GET /api/plugins lists what is",
        )
    _ = allow_restart
    config = channel.config
    running = (await state.supervisor.containers(name)).composer.running
    data = config.model_dump(mode="json")
    data["visualization"]["active"] = body.active
    if changed:
        save_channel_config(channel.directory, ChannelConfig.model_validate(data))
        try:
            recompile(state, name, preserve_visualizer_accent=running)
        except ApiError:
            save_channel_config(channel.directory, config)
            raise

    recreate = changed and running and config.visualization.enabled
    generation = (
        queue_visualizer_action(
            state, name, "recreate", event_data={"active": body.active}
        )
        if recreate
        else None
    )
    if recreate:
        response.status_code = 202
    else:
        await publish_visualization_event(
            state,
            name,
            event_state="saved",
            applied=True,
            active=body.active,
        )
    return {
        "accepted": changed,
        "channel": name,
        "active": body.active,
        "hot_set": list(config.visualization.hot_set),
        "staged": False,
        "restarted": False,
        "visualizer_recreated": recreate,
        "generation": generation,
        "mode": "visualizer-recreate" if recreate else "applied-on-next-start",
        "detail": (
            "the isolated visualizer is being replaced; composer and ingest are unchanged"
            if recreate
            else "saved; it applies when the visualizer next starts"
        ),
    }


@router.put("/channels/{name}/hot-set")
async def set_hot_set(
    name: str, body: HotSetBody, response: Response, state: AppState = VisualMutation
) -> dict[str, Any]:
    """Preserve the deprecated field for compatibility; it has no runtime effect."""
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    requested = list(dict.fromkeys(body.hot_set))

    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    active = body.active or config.visualization.active
    if body.active is not None and active not in registry:
        raise ApiError(
            404, "unknown_plugin",
            f"{active!r} is not installed; GET /api/plugins lists what is",
        )

    if requested == list(config.visualization.hot_set) and active == config.visualization.active:
        return {
            "accepted": False, "channel": name, "hot_set": requested, "active": active,
            "restarted": False, "detail": f"{name} already instantiates exactly that set",
        }

    data = config.model_dump(mode="json")
    data["visualization"]["hot_set"] = requested
    data["visualization"]["active"] = active
    running = (await state.supervisor.containers(name)).composer.running
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))
    try:
        recompile(state, name, preserve_visualizer_accent=running)
    except ApiError:
        save_channel_config(channel.directory, config)
        raise

    recreate = (
        active != config.visualization.active
        and running
        and config.visualization.enabled
    )
    generation = (
        queue_visualizer_action(
            state,
            name,
            "recreate",
            event_data={"active": active, "hot_set": requested},
        )
        if recreate
        else None
    )
    if recreate:
        response.status_code = 202
    else:
        await publish_visualization_event(
            state,
            name,
            event_state="saved",
            applied=True,
            active=active,
            hot_set=requested,
        )
    return {
        "accepted": True,
        "channel": name,
        "hot_set": requested,
        "active": active,
        "restarted": False,
        "visualizer_recreated": recreate,
        "generation": generation,
        "detail": (
            "deprecated hot_set saved; active plugin queued for visualizer replacement"
            if recreate
            else "deprecated hot_set saved and ignored by the runtime"
        ),
    }


@router.put("/channels/{name}/visualization/visible", status_code=202)
async def set_visible(
    name: str, body: VisibleBody, state: AppState = VisualMutation
) -> dict[str, Any]:
    """Take the visualization overlay off air without stopping its child."""
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    if not config.visualization.enabled:
        raise ApiError(
            409, "visualization_not_built",
            f"{name} has no running visualizer to reveal; enable it first",
        )

    if body.visible != config.visualization.visible:
        data = config.model_dump(mode="json")
        data["visualization"]["visible"] = body.visible
        save_channel_config(channel.directory, ChannelConfig.model_validate(data))
        recompile(state, name)

    running = (await state.supervisor.containers(name)).composer.running
    sent = False
    if running:
        try:
            message = build_message(OVERLAY_TARGET, "enable", "1" if body.visible else "0")
        except ZmqValidationError as exc:
            raise ApiError(400, "invalid_command", str(exc)) from exc
        sent = await _send(state, name, [message])

    detail = (
        "switched on the running graph; one frame, no gap"
        if sent
        else "saved; it applies when the channel next starts"
    )
    await publish_visualization_event(
        state,
        name,
        event_state="applied" if sent else "saved",
        applied=sent,
        detail=detail,
        visible=body.visible,
    )
    return {
        "accepted": True,
        "channel": name,
        "visible": body.visible,
        "live": sent,
        "detail": detail,
    }


@router.put("/channels/{name}/visualization/opacity", status_code=202)
async def set_opacity(
    name: str, body: OpacityBody, state: AppState = VisualMutation
) -> dict[str, Any]:
    """Change isolated-layer opacity on the running compositor."""
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    if body.opacity != config.visualization.opacity:
        data = config.model_dump(mode="json")
        data["visualization"]["opacity"] = body.opacity
        save_channel_config(channel.directory, ChannelConfig.model_validate(data))
        recompile(state, name)

    running = (await state.supervisor.containers(name)).composer.running
    sent = False
    if running:
        expression = f"(val-16)*1.16438356*{body.opacity:g}"
        try:
            message = build_message(OPACITY_TARGET, "y", expression)
        except ZmqValidationError as exc:
            raise ApiError(400, "invalid_command", str(exc)) from exc
        sent = await _send(state, name, [message])

    detail = (
        "opacity changed on the running compositor; one frame, no gap"
        if sent
        else "saved; it applies when the channel next starts"
    )
    await publish_visualization_event(
        state,
        name,
        event_state="applied" if sent else "saved",
        applied=sent,
        detail=detail,
        opacity=body.opacity,
    )
    return {
        "accepted": True,
        "channel": name,
        "opacity": body.opacity,
        "live": sent,
        "detail": detail,
    }


@router.put("/channels/{name}/visualization/parameters", status_code=202)
async def set_parameters(
    name: str, body: ParametersBody, response: Response, state: AppState = VisualMutation
) -> dict[str, Any]:
    """Tune one plugin, recreating only the child when it is active."""
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    manifest = registry.get(body.plugin)
    if manifest is None:
        raise ApiError(
            404, "unknown_plugin", f"{body.plugin!r} is not installed; GET /api/plugins lists what is"
        )

    if manifest is not None:
        declared = {p.name for p in manifest.parameters}
        unknown = sorted(set(body.values) - declared)
        if unknown:
            raise ApiError(
                400, "unknown_parameter",
                f"{body.plugin} has no parameter(s) {', '.join(unknown)}; it declares "
                f"{', '.join(sorted(declared)) or 'none'}",
            )
        try:
            # Store the clamped values, so what the operator reads back is what
            # the graph will actually be built with.
            resolved = manifest.resolve_parameters({**body.values})
        except plugin_registry.PluginError as exc:
            raise ApiError(400, "invalid_parameter", str(exc)) from exc
    data = config.model_dump(mode="json")
    parameters = dict(data["visualization"].get("parameters") or {})
    parameters[body.plugin] = resolved
    data["visualization"]["parameters"] = parameters
    running = (await state.supervisor.containers(name)).composer.running
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))
    recompile(state, name, preserve_visualizer_accent=running)

    drawing = (
        running
        and config.visualization.enabled
        and body.plugin == config.visualization.active
    )
    generation = None
    if drawing:
        generation = queue_visualizer_action(
            state,
            name,
            "recreate",
            event_data={"plugin": body.plugin, "values": resolved},
        )
    else:
        response.status_code = 200
        await publish_visualization_event(
            state,
            name,
            event_state="saved",
            applied=True,
            plugin=body.plugin,
            values=resolved,
        )
    return {
        "accepted": True,
        "channel": name,
        "plugin": body.plugin,
        "values": resolved,
        "restarted": False,
        "visualizer_recreated": drawing,
        "generation": generation,
        "detail": (
            "the isolated visualizer is being replaced; composer and ingest are unchanged"
            if drawing
            else "saved; it applies the next time that plugin is active"
        ),
    }


@router.post("/channels/{name}/preset", status_code=202)
async def post_preset(name: str, body: PresetBody, state: AppState = Authed) -> dict[str, Any]:
    application = await apply_preset_to_channel(state, name, body.preset)
    return {
        "accepted": True,
        "channel": name,
        "preset": body.preset,
        "changed": application.changed,
        "commands": len(application.messages),
    }


@router.put("/channels/{name}/color")
async def set_color(
    name: str, body: ColorBody, state: AppState = VisualMutation
) -> dict[str, Any]:
    _cancel_color_mode_task(state, name)
    channel = state.channel(name, resolve_media=False)
    previous_color = channel.config.color
    was_manual = previous_color.mode is ColorMode.MANUAL
    current: preset_registry.ColorTargets | None = None
    current_accent = ""
    current_profile: colorprofile.ColorProfile | None = None
    if was_manual:
        current = preset_registry.color_targets(
            previous_color.manual.accent, previous_color.manual.tint
        )
        current_accent = previous_color.manual.accent
    else:
        current_profile = await _current_slide_profile(state, channel)
        if current_profile is not None:
            current = preset_registry.automatic_color_targets(
                current_profile.accent,
                current_profile.brightness,
                current_profile.warmth,
            )
            current_accent = current_profile.accent
    stream_time = await _stream_time(state, name)
    baked_accent = _baked_accent(state, name)
    current, current_rotation = _sample_color_ramp(
        state,
        name,
        current,
        current_accent,
        baked_accent,
        stream_time,
    )
    data = channel.config.model_dump(mode="json")
    color = data["color"]
    if body.mode is not None:
        color["mode"] = body.mode.value
    if body.manual is not None:
        color["manual"] = body.manual.model_dump(mode="json")
    if body.transition_seconds is not None:
        color["transition_seconds"] = body.transition_seconds
    config = ChannelConfig.model_validate(data)
    save_channel_config(channel.directory, config)
    if config.color.mode is ColorMode.MANUAL and not was_manual:
        _publish_color_mode(state, name, ColorMode.MANUAL)
        await asyncio.sleep(COLOR_MODE_SETTLE_SECONDS)

    messages: list[str] = []
    detail = ""
    target: preset_registry.ColorTargets | None = None
    target_accent = ""
    if config.color.mode is ColorMode.MANUAL:
        target = preset_registry.color_targets(
            config.color.manual.accent, config.color.manual.tint
        )
        target_accent = config.color.manual.accent
        messages = preset_registry.color_messages(
            config.color.manual.accent,
            config.color.manual.tint,
            transition_seconds=config.color.transition_seconds,
            stream_time=stream_time,
            current=current,
            current_accent=current_accent,
            current_viz_rotation=current_rotation,
            baked_accent=baked_accent,
            target=target,
        )
    elif was_manual:
        # The producer only re-colors on a slide change, which on a one-image
        # channel may never come.
        profile = current_profile or await _current_slide_profile(state, channel)
        if profile is None:
            detail = (
                "switched to automatic, but the current slide has no color profile; "
                "the manual color stays until the next slide change"
            )
        else:
            target = preset_registry.automatic_color_targets(
                profile.accent, profile.brightness, profile.warmth
            )
            target_accent = profile.accent
            messages = preset_registry.color_messages(
                profile.accent,
                profile.dominant,
                transition_seconds=config.color.transition_seconds,
                stream_time=stream_time,
                current=current,
                current_accent=current_accent,
                current_viz_rotation=current_rotation,
                baked_accent=baked_accent,
                target=target,
            )
            detail = f"switched to automatic and re-applied {profile.accent} from the current slide"
    delivered = await _send(state, name, messages)
    if delivered and target is not None:
        _remember_color_ramp(
            state,
            name,
            current or preset_registry.NEUTRAL_TARGETS,
            current_rotation,
            target,
            target_accent,
            baked_accent,
            stream_time,
            config.color.transition_seconds,
        )
    if config.color.mode is ColorMode.AUTOMATIC and was_manual and (
        delivered or target is None
    ):
        _schedule_color_mode(
            state,
            name,
            ColorMode.AUTOMATIC,
            config.color.transition_seconds if delivered else 0.0,
        )
    return {
        "channel": name,
        "color": config.color.model_dump(mode="json"),
        "commands": messages,
        "detail": detail,
    }


def _baked_accent(state: AppState, name: str) -> str:
    """The accent the compositor built its plugins with, published at launch.

    `hue` rotates rather than sets, so a requested color is only reachable as a
    rotation away from whatever the graph was actually built with.
    """
    if not state.channel(name, resolve_media=False).config.visualization.enabled:
        return ""
    try:
        value = (state.workspace.run_dir / name / "viz-accent").read_text(encoding="utf-8")
    except OSError:
        return ""
    return value.strip()


def _sample_color_ramp(
    state: AppState,
    name: str,
    fallback: preset_registry.ColorTargets | None,
    fallback_accent: str,
    baked_accent: str,
    stream_time: float | None,
) -> tuple[preset_registry.ColorTargets | None, float | None]:
    rotation = (
        preset_registry.viz_rotation(baked_accent, fallback_accent)
        if baked_accent and fallback_accent
        else None
    )
    ramp = state.color_ramps.get(name)
    if ramp is None or stream_time is None:
        return fallback, rotation
    if stream_time < ramp.started_at or time.monotonic() > (
        ramp.created_at + ramp.seconds + 1.0
    ):
        state.color_ramps.pop(name, None)
        return fallback, rotation
    targets, sampled_rotation = ramp.sample(stream_time)
    return targets, preset_registry.canonical_rotation(sampled_rotation)


def _remember_color_ramp(
    state: AppState,
    name: str,
    current: preset_registry.ColorTargets,
    current_rotation: float | None,
    target: preset_registry.ColorTargets,
    target_accent: str,
    baked_accent: str,
    stream_time: float | None,
    seconds: float,
) -> None:
    if stream_time is None:
        state.color_ramps.pop(name, None)
        return
    start_rotation = current_rotation or 0.0
    target_rotation = start_rotation
    if baked_accent and target_accent:
        target_rotation = preset_registry.nearest_rotation(
            start_rotation,
            preset_registry.viz_rotation(baked_accent, target_accent),
        )
    state.color_ramps[name] = preset_registry.ColorRamp(
        start=current,
        target=target,
        start_rotation=start_rotation,
        target_rotation=target_rotation,
        started_at=stream_time,
        seconds=seconds,
        created_at=time.monotonic(),
    )


def _publish_color_mode(state: AppState, name: str, mode: ColorMode) -> None:
    """Tell the running producer the mode without restarting the compositor.

    The producer reads `COLOR_MODE` at launch, so without this a switch to
    manual would not stop it re-coloring until the composer restarted — the one
    thing this design exists to avoid. The run directory is shared with the
    container, so an atomic replace here is visible there immediately.
    """
    directory = state.workspace.run_dir / name
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "color-mode"
        temp = directory / "color-mode.tmp"
        temp.write_text(f"{'manual' if mode is ColorMode.MANUAL else 'auto'}\n", encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        LOG.warning("color mode for %s not published: %s", name, exc)


def _cancel_color_mode_task(state: AppState, name: str) -> None:
    task = state.color_mode_tasks.pop(name, None)
    if task is not None:
        task.cancel()


def _schedule_color_mode(
    state: AppState, name: str, mode: ColorMode, delay: float
) -> None:
    async def publish() -> None:
        try:
            if delay > 0.0:
                await asyncio.sleep(delay)
            _publish_color_mode(state, name, mode)
        except asyncio.CancelledError:
            return
        finally:
            if state.color_mode_tasks.get(name) is asyncio.current_task():
                state.color_mode_tasks.pop(name, None)

    state.color_mode_tasks[name] = asyncio.create_task(publish())


async def _current_slide_profile(
    state: AppState, channel: ResolvedChannel
) -> colorprofile.ColorProfile | None:
    now = parse_now_json(await state.supervisor.read_run_file(channel.name, "now.json"))
    slide = now.get("current_slide")
    if not isinstance(slide, str) or not slide:
        return None
    located = _slide_image(state, channel, slide)
    if located is None:
        return None
    tree, image = located
    return colorprofile.read_profile(colorprofile.profile_path(image, tree))


def _slide_image(
    state: AppState, channel: ResolvedChannel, slide: str
) -> tuple[Path, Path] | None:
    """now.json names a repo-relative slide; keep it inside the two media trees."""
    parts = PurePosixPath(slide).parts
    if slide.startswith("/") or ".." in parts:
        return None
    if parts[:1] == ("common",):
        tree, relative = state.workspace.common_dir, parts[1:]
    elif parts[:2] == ("channels", channel.name):
        tree, relative = channel.directory, parts[2:]
    else:
        return None
    if not relative:
        return None
    image = Path(tree, *relative)
    try:
        image.resolve().relative_to(Path(tree).resolve())
    except (OSError, ValueError):
        return None
    return Path(tree), image


async def apply_preset_to_channel(
    state: AppState, name: str, preset_name: str
) -> preset_registry.PresetApplication:
    async with state.visualization_lock(name):
        return await _apply_preset_to_channel_locked(state, name, preset_name)


async def _apply_preset_to_channel_locked(
    state: AppState, name: str, preset_name: str
) -> preset_registry.PresetApplication:
    _cancel_color_mode_task(state, name)
    channel = state.channel(name, resolve_media=False)
    was_manual = channel.config.color.mode is ColorMode.MANUAL
    preset = preset_registry.get_preset(state.workspace.presets_dir, preset_name)
    current: preset_registry.ColorTargets | None = None
    current_accent = ""
    if channel.config.color.mode is ColorMode.MANUAL:
        current = preset_registry.color_targets(
            channel.config.color.manual.accent, channel.config.color.manual.tint
        )
        current_accent = channel.config.color.manual.accent
    else:
        profile = await _current_slide_profile(state, channel)
        if profile is not None:
            current = preset_registry.automatic_color_targets(
                profile.accent, profile.brightness, profile.warmth
            )
            current_accent = profile.accent
    stream_time = await _stream_time(state, name)
    baked_accent = _baked_accent(state, name)
    current, current_rotation = _sample_color_ramp(
        state,
        name,
        current,
        current_accent,
        baked_accent,
        stream_time,
    )
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    if (
        preset.visualization is not None
        and preset.visualization.active != channel.config.visualization.active
        and preset.visualization.active not in registry
    ):
        raise ApiError(
            404,
            "unknown_plugin",
            f"{preset.visualization.active!r} is not installed; GET /api/plugins lists what is",
        )
    application = preset_registry.apply_preset(
        preset,
        channel.config,
        stream_time=stream_time,
        current=current,
        current_accent=current_accent,
        current_viz_rotation=current_rotation,
        baked_accent=baked_accent,
    )

    running = (await state.supervisor.containers(name)).composer.running
    save_channel_config(channel.directory, application.config)
    is_manual = application.config.color.mode is ColorMode.MANUAL
    target: preset_registry.ColorTargets | None = None
    target_accent = ""
    if is_manual:
        target = preset_registry.color_targets(
            application.config.color.manual.accent,
            application.config.color.manual.tint,
        )
        target_accent = application.config.color.manual.accent
    if is_manual and not was_manual:
        _publish_color_mode(state, name, ColorMode.MANUAL)
        await asyncio.sleep(COLOR_MODE_SETTLE_SECONDS)
    if application.rewrite_images_list or application.visualization is not None:
        recompile(
            state,
            name,
            preserve_visualizer_accent=(
                running and application.visualization is not None
            ),
        )

    if was_manual and not is_manual:
        profile = await _current_slide_profile(state, channel)
        if profile is not None:
            target = preset_registry.automatic_color_targets(
                profile.accent, profile.brightness, profile.warmth
            )
            target_accent = profile.accent
            application.messages.extend(
                preset_registry.color_messages(
                    profile.accent,
                    profile.dominant,
                    transition_seconds=application.config.color.transition_seconds,
                    stream_time=stream_time,
                    current=current,
                    current_accent=current_accent,
                    current_viz_rotation=current_rotation,
                    baked_accent=baked_accent,
                    target=target,
                )
            )

    delivered = await _send(state, name, application.messages)
    if delivered and target is not None:
        _remember_color_ramp(
            state,
            name,
            current or preset_registry.NEUTRAL_TARGETS,
            current_rotation,
            target,
            target_accent,
            baked_accent,
            stream_time,
            application.config.color.transition_seconds,
        )
    if not is_manual and was_manual and (delivered or target is None):
        _schedule_color_mode(
            state,
            name,
            ColorMode.AUTOMATIC,
            application.config.color.transition_seconds if delivered else 0.0,
        )
    if application.visualization is not None:
        if running and application.config.visualization.enabled:
            queue_visualizer_action(
                state,
                name,
                "recreate",
                event_data={"active": application.visualization},
            )
        else:
            await publish_visualization_event(
                state,
                name,
                event_state="saved",
                applied=True,
                active=application.visualization,
            )
    return application


async def _stream_time(state: AppState, name: str) -> float | None:
    """Color ramps are expressions in `t`, which is stream time, not wallclock."""
    sample = parse_progress(await state.supervisor.read_run_file(name, "progress"))
    return round(sample.out_time_seconds, 3) if sample else None


async def _send(state: AppState, name: str, messages: list[str]) -> bool:
    """Best effort: a stopped channel keeps the persisted config change."""
    if not messages:
        return False
    try:
        await state.supervisor.send_zmq_batch(name, messages)
    except (ZmqCommandError, ZmqValidationError, OSError) as exc:
        LOG.info("channel %s: runtime command not delivered: %s", name, exc)
        return False
    return True
