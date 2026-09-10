"""Plugins, presets, visualization switching and color.

A plugin in `hot_set` switches instantly: its branch is already rendering and
`streamselect` picks it in one frame. An installed plugin that is not hot is
still usable — it is staged into `hot_set` and the compositor is replaced
make-before-break, which costs a real gap of seconds. Only a plugin that is not
installed at all is an error.
"""

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
from ..events import CHANNEL_VISUALIZATION
from ..main import ApiError, AppState
from ..models import ChannelConfig, ColorMode, ManualColor, StrictModel
from ..watchdog import parse_progress
from ..zmqctl import ZmqCommandError, ZmqValidationError, build_message, stream_select_message
from .deps import Authed, parse_now_json, recompile, run_action, save_channel_config

LOG = logging.getLogger("ambient.api.looks")

router = APIRouter(prefix="/api", tags=["looks"])

STREAMSELECT_TARGET = "streamselect@sel"
# Timeline `enable` on the composite, which is what makes standby one frame.
OVERLAY_TARGET = "overlay@viz"
COLOR_MODE_SETTLE_SECONDS = 0.6


class VisualizationBody(StrictModel):
    active: str


class HotSetBody(StrictModel):
    hot_set: list[str] = Field(..., min_length=1)
    # Optional: only needed when the change drops the branch currently on air.
    active: str | None = None


class VisibleBody(StrictModel):
    visible: bool


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
        True, description="stage a plugin outside hot_set; false refuses instead"
    ),
    state: AppState = Authed,
) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    hot_set = list(channel.config.visualization.hot_set)

    if body.active in hot_set:
        return await _switch_hot(state, channel.directory, name, channel.config, body.active)

    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    if registry and body.active not in registry:
        raise ApiError(
            404,
            "unknown_plugin",
            f"{body.active!r} is not installed; GET /api/plugins lists what is",
        )
    if not allow_restart:
        raise ApiError(
            409,
            "restart_required",
            f"{body.active!r} is installed but not in hot_set on {name}; a filtergraph "
            "is fixed at launch, so staging it needs a make-before-break restart. "
            "Retry with allow_restart=true.",
        )

    response.status_code = 202
    return await _stage(state, channel, body.active)


async def _switch_hot(
    state: AppState, directory: Path, name: str, config: ChannelConfig, active: str
) -> dict[str, Any]:
    """One frame, clean cut: the branch is already rendering."""
    hot_set = list(config.visualization.hot_set)
    if active != config.visualization.active:
        data = config.model_dump(mode="json")
        data["visualization"]["active"] = active
        save_channel_config(directory, ChannelConfig.model_validate(data))
        try:
            message = stream_select_message(
                STREAMSELECT_TARGET, hot_set.index(active), len(hot_set)
            )
        except ZmqValidationError as exc:
            raise ApiError(400, "invalid_command", str(exc)) from exc
        await _send(state, name, [message])
        await state.events.publish(CHANNEL_VISUALIZATION, {"active": active}, channel=name)
    return {
        "channel": name,
        "active": active,
        "hot_set": hot_set,
        "staged": False,
        "restarted": False,
        "mode": "streamselect",
        "detail": "switched on the running filtergraph; no gap",
    }


async def _stage(
    state: AppState, channel: ResolvedChannel, active: str
) -> dict[str, Any]:
    """Add the branch to the graph, then replace the compositor make-before-break."""
    name = channel.name
    config = channel.config
    data = config.model_dump(mode="json")
    hot_set = list(config.visualization.hot_set) + [active]
    data["visualization"]["hot_set"] = hot_set
    data["visualization"]["active"] = active

    save_channel_config(channel.directory, ChannelConfig.model_validate(data))
    try:
        recompile(state, name)
    except ApiError:
        save_channel_config(channel.directory, config)
        raise

    reloaded = state.channel(name, resolve_media=False)
    running = (await state.supervisor.containers(name)).composer.running
    if running:
        asyncio.create_task(
            run_action(state, name, state.supervisor.restart(name), "visualization")
        )
    await state.events.publish(
        CHANNEL_VISUALIZATION, {"active": active, "staged": True}, channel=name
    )
    return {
        "accepted": True,
        "channel": name,
        "active": active,
        "hot_set": hot_set,
        "staged": True,
        "restarted": running,
        "mode": "make-before-break" if running else "applied-on-next-start",
        "projected_cores": round(reloaded.projected_cores, 3),
        "detail": (
            f"{active!r} was not instantiated, so the compositor is being replaced with a "
            "graph that includes it; measured ~1s of RTMP gap and a new YouTube ingest "
            "session"
            if running
            else f"{active!r} was staged into hot_set; it applies on the next start"
        ),
    }


@router.put("/channels/{name}/hot-set", status_code=202)
async def set_hot_set(name: str, body: HotSetBody, state: AppState = Authed) -> dict[str, Any]:
    """Choose which branches the graph instantiates.

    Not a live change in either direction: a filtergraph is fixed at launch, so
    both adding and removing a branch replaces the compositor. Removing is the
    only way to give idle-branch cores back, which is why this endpoint exists
    at all — switching a plugin in can only ever grow the set.
    """
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    requested = list(dict.fromkeys(body.hot_set))

    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    if registry:
        missing = [p for p in requested if p not in registry]
        if missing:
            raise ApiError(
                404, "unknown_plugin",
                f"not installed: {', '.join(sorted(missing))}; GET /api/plugins lists what is",
            )

    # Dropping the branch that is on air would leave `active` pointing at nothing,
    # so the caller must say what replaces it rather than have one chosen for them.
    active = body.active or config.visualization.active
    if active not in requested:
        raise ApiError(
            409, "active_not_in_hot_set",
            f"{active!r} is on air but not in the requested hot set; pass `active` to say "
            "which branch takes over",
        )

    if requested == list(config.visualization.hot_set) and active == config.visualization.active:
        return {
            "accepted": False, "channel": name, "hot_set": requested, "active": active,
            "restarted": False, "detail": f"{name} already instantiates exactly that set",
        }

    data = config.model_dump(mode="json")
    data["visualization"]["hot_set"] = requested
    data["visualization"]["active"] = active
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))
    try:
        reloaded = state.channel(name, resolve_media=False)
        recompile(state, name)
    except ApiError:
        save_channel_config(channel.directory, config)
        raise

    running = (await state.supervisor.containers(name)).composer.running
    if running and config.visualization.enabled:
        asyncio.create_task(run_action(state, name, state.supervisor.restart(name), "hot_set"))
    await state.events.publish(
        CHANNEL_VISUALIZATION, {"active": active, "hot_set": requested}, channel=name
    )
    return {
        "accepted": True,
        "channel": name,
        "hot_set": requested,
        "active": active,
        # A channel drawing nothing has no branches in its graph, so changing which
        # ones it would instantiate costs it nothing until the visualization is on.
        "restarted": running and config.visualization.enabled,
        "projected_cores": round(reloaded.projected_cores, 3),
        "detail": (
            "the compositor is being replaced with a graph containing exactly these "
            "branches; measured ~1s of RTMP gap and a new YouTube ingest session"
            if running and config.visualization.enabled
            else "applies on the next start"
        ),
    }


@router.put("/channels/{name}/visualization/visible", status_code=202)
async def set_visible(name: str, body: VisibleBody, state: AppState = Authed) -> dict[str, Any]:
    """Standby: take the visualization off air and put it back with no restart.

    This is the one on/off that is live. `visualization.enabled` decides whether
    the branches exist at all and cannot change without rebuilding the graph;
    this rides `overlay`'s timeline `enable`, which is one frame. The branches
    keep rendering either way, so standby costs what on costs — that is the
    price of being able to toggle at all.
    """
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    if not config.visualization.enabled:
        raise ApiError(
            409, "visualization_not_built",
            f"{name} has no visualization branches in its graph, so there is nothing to "
            "reveal; set visualization.enabled and restart first",
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
        await _send(state, name, [message])
        sent = True

    await state.events.publish(
        CHANNEL_VISUALIZATION, {"visible": body.visible}, channel=name
    )
    return {
        "accepted": True,
        "channel": name,
        "visible": body.visible,
        "live": sent,
        "detail": (
            "switched on the running graph; one frame, no gap"
            if sent
            else "saved; it applies when the channel next starts"
        ),
    }


@router.put("/channels/{name}/visualization/parameters", status_code=202)
async def set_parameters(
    name: str, body: ParametersBody, state: AppState = Authed
) -> dict[str, Any]:
    """Tune one plugin's knobs.

    Substituted into the fragment at launch, so this restarts a channel that is
    actually drawing the plugin. A plugin nobody is looking at is just saved.
    """
    channel = state.channel(name, resolve_media=False)
    config = channel.config
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    manifest = registry.get(body.plugin)
    if registry and manifest is None:
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
    else:
        resolved = dict(body.values)

    data = config.model_dump(mode="json")
    parameters = dict(data["visualization"].get("parameters") or {})
    parameters[body.plugin] = resolved
    data["visualization"]["parameters"] = parameters
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))
    recompile(state, name)

    running = (await state.supervisor.containers(name)).composer.running
    drawing = (
        running
        and config.visualization.enabled
        and body.plugin in config.visualization.hot_set
    )
    if drawing:
        asyncio.create_task(run_action(state, name, state.supervisor.restart(name), "parameters"))
    return {
        "accepted": True,
        "channel": name,
        "plugin": body.plugin,
        "values": resolved,
        "restarted": drawing,
        "detail": (
            "the compositor is being replaced to rebuild that branch; measured ~1s of "
            "RTMP gap and a new YouTube ingest session"
            if drawing
            else "saved; it applies the next time that branch is built"
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
async def set_color(name: str, body: ColorBody, state: AppState = Authed) -> dict[str, Any]:
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
    try:
        application = preset_registry.apply_preset(
            preset,
            channel.config,
            stream_time=stream_time,
            current=current,
            current_accent=current_accent,
            current_viz_rotation=current_rotation,
            baked_accent=baked_accent,
        )
    except preset_registry.NotInHotSet as exc:
        raise ApiError(409, "not_in_hot_set", str(exc)) from exc

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
    if application.rewrite_images_list:
        recompile(state, name)

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

    if application.visualization is not None:
        hot_set = application.config.visualization.hot_set
        application.messages.insert(
            0,
            stream_select_message(
                STREAMSELECT_TARGET, hot_set.index(application.visualization), len(hot_set)
            ),
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
        await state.events.publish(
            CHANNEL_VISUALIZATION, {"active": application.visualization}, channel=name
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
