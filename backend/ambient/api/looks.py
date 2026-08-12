"""Plugins, presets, visualization switching and color.

A plugin in `hot_set` switches instantly: its branch is already rendering and
`streamselect` picks it in one frame. An installed plugin that is not hot is
still usable — it is staged into `hot_set` and the compositor is replaced
make-before-break, which costs a measured ~1s gap. Only a plugin that is not
installed at all is an error.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import Field

from .. import plugins as plugin_registry
from .. import presets as preset_registry
from ..config import ResolvedChannel
from ..events import CHANNEL_VISUALIZATION
from ..main import ApiError, AppState
from ..models import ChannelConfig, ColorMode, ManualColor, StrictModel
from ..watchdog import parse_progress
from ..zmqctl import ZmqCommandError, ZmqValidationError, stream_select_message
from .deps import Authed, recompile, run_action, save_channel_config

LOG = logging.getLogger("ambient.api.looks")

router = APIRouter(prefix="/api", tags=["looks"])

STREAMSELECT_TARGET = "streamselect@sel"


class VisualizationBody(StrictModel):
    active: str


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
    channel = state.channel(name, resolve_media=False)
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

    messages: list[str] = []
    if config.color.mode is ColorMode.MANUAL:
        messages = preset_registry.color_messages(
            config.color.manual.accent,
            config.color.manual.tint,
            transition_seconds=config.color.transition_seconds,
            stream_time=await _stream_time(state, name),
        )
        await _send(state, name, messages)
    return {
        "channel": name,
        "color": config.color.model_dump(mode="json"),
        "commands": messages,
    }


async def apply_preset_to_channel(
    state: AppState, name: str, preset_name: str
) -> preset_registry.PresetApplication:
    channel = state.channel(name, resolve_media=False)
    preset = preset_registry.get_preset(state.workspace.presets_dir, preset_name)
    try:
        application = preset_registry.apply_preset(
            preset, channel.config, stream_time=await _stream_time(state, name)
        )
    except preset_registry.NotInHotSet as exc:
        raise ApiError(409, "not_in_hot_set", str(exc)) from exc

    save_channel_config(channel.directory, application.config)
    if application.rewrite_images_list:
        recompile(state, name)

    if application.visualization is not None:
        hot_set = application.config.visualization.hot_set
        application.messages.insert(
            0,
            stream_select_message(
                STREAMSELECT_TARGET, hot_set.index(application.visualization), len(hot_set)
            ),
        )
    await _send(state, name, application.messages)
    if application.visualization is not None:
        await state.events.publish(
            CHANNEL_VISUALIZATION, {"active": application.visualization}, channel=name
        )
    return application


async def _stream_time(state: AppState, name: str) -> float:
    """Color ramps are expressions in `t`, which is stream time, not wallclock."""
    verdict = state.watchdog.latest.get(name)
    if verdict is not None and verdict.sample is not None:
        return round(verdict.sample.out_time_seconds, 3)
    sample = parse_progress(await state.supervisor.read_run_file(name, "progress"))
    return round(sample.out_time_seconds, 3) if sample else 0.0


async def _send(state: AppState, name: str, messages: list[str]) -> None:
    """Best effort: a stopped channel keeps the persisted config change."""
    if not messages:
        return
    try:
        await state.supervisor.send_zmq_batch(name, messages)
    except (ZmqCommandError, ZmqValidationError, OSError) as exc:
        LOG.info("channel %s: runtime command not delivered: %s", name, exc)
