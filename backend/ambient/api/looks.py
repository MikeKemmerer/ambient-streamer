"""Plugins, presets, visualisation switching and colour.

Switching to a plugin outside `hot_set` is a `409`, never a silent promotion:
promoting it means a new filtergraph, which means a restart the caller did not
ask for.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import Field

from .. import plugins as plugin_registry
from .. import presets as preset_registry
from ..events import CHANNEL_VISUALISATION
from ..main import ApiError, AppState
from ..models import ChannelConfig, ColourMode, ManualColour, StrictModel
from ..watchdog import parse_progress
from ..zmqctl import ZmqCommandError, ZmqValidationError, stream_select_message
from .deps import Authed, recompile, save_channel_config

LOG = logging.getLogger("ambient.api.looks")

router = APIRouter(prefix="/api", tags=["looks"])

STREAMSELECT_TARGET = "streamselect@sel"


class VisualisationBody(StrictModel):
    active: str


class PresetBody(StrictModel):
    preset: str


class ColourBody(StrictModel):
    mode: ColourMode | None = None
    manual: ManualColour | None = None
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
                "visualisation": preset.visualisation.active if preset.visualisation else None,
                "colour": preset.colour.model_dump(mode="json") if preset.colour else None,
                "slideshow": preset.slideshow.model_dump(mode="json", exclude_none=True),
                "audio": preset.audio.model_dump(mode="json", exclude_none=True),
            }
            for preset in registry.values()
        ]
    }


@router.put("/channels/{name}/visualisation")
async def set_visualisation(
    name: str, body: VisualisationBody, state: AppState = Authed
) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    hot_set = channel.config.visualisation.hot_set
    if body.active not in hot_set:
        raise ApiError(
            409,
            "not_in_hot_set",
            f"{body.active!r} is not instantiated on {name}; promoting it needs a restart",
        )

    if body.active != channel.config.visualisation.active:
        data = channel.config.model_dump(mode="json")
        data["visualisation"]["active"] = body.active
        save_channel_config(channel.directory, ChannelConfig.model_validate(data))
        try:
            message = stream_select_message(
                STREAMSELECT_TARGET, hot_set.index(body.active), len(hot_set)
            )
        except ZmqValidationError as exc:
            raise ApiError(400, "invalid_command", str(exc)) from exc
        await _send(state, name, [message])
        await state.events.publish(
            CHANNEL_VISUALISATION, {"active": body.active}, channel=name
        )
    return {"channel": name, "active": body.active, "hot_set": hot_set}


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


@router.put("/channels/{name}/colour")
async def set_colour(name: str, body: ColourBody, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    data = channel.config.model_dump(mode="json")
    colour = data["colour"]
    if body.mode is not None:
        colour["mode"] = body.mode.value
    if body.manual is not None:
        colour["manual"] = body.manual.model_dump(mode="json")
    if body.transition_seconds is not None:
        colour["transition_seconds"] = body.transition_seconds
    config = ChannelConfig.model_validate(data)
    save_channel_config(channel.directory, config)

    messages: list[str] = []
    if config.colour.mode is ColourMode.MANUAL:
        messages = preset_registry.colour_messages(
            config.colour.manual.accent,
            config.colour.manual.tint,
            transition_seconds=config.colour.transition_seconds,
            stream_time=await _stream_time(state, name),
        )
        await _send(state, name, messages)
    return {
        "channel": name,
        "colour": config.colour.model_dump(mode="json"),
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

    if application.visualisation is not None:
        hot_set = application.config.visualisation.hot_set
        application.messages.insert(
            0,
            stream_select_message(
                STREAMSELECT_TARGET, hot_set.index(application.visualisation), len(hot_set)
            ),
        )
    await _send(state, name, application.messages)
    if application.visualisation is not None:
        await state.events.publish(
            CHANNEL_VISUALISATION, {"active": application.visualisation}, channel=name
        )
    return application


async def _stream_time(state: AppState, name: str) -> float:
    """Colour ramps are expressions in `t`, which is stream time, not wallclock."""
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
