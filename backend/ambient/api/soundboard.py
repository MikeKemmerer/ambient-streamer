"""Manual sound effects mixed over a running channel's music."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from starlette.responses import FileResponse

from .. import liqctl
from ..main import ApiError, AppState
from ..media import MediaRoots, discover_soundboard
from ..models import StrictModel
from .deps import Authed

router = APIRouter(prefix="/api/channels", tags=["soundboard"])


class SoundboardPlayBody(StrictModel):
    clip: str


def _relative(state: AppState, path: Path) -> str:
    try:
        return path.relative_to(state.workspace.root).as_posix()
    except ValueError:
        return str(path)


def _clips(state: AppState, name: str):
    channel = state.channel(name, resolve_media=False)
    roots = MediaRoots.create(
        state.workspace.root,
        state.workspace.common_dir,
        channel.directory,
    )
    return channel, discover_soundboard(roots)


def _selected_clip(state: AppState, name: str, container_path: str):
    channel, selection = _clips(state, name)
    item = next(
        (clip for clip in selection.files if clip.container_path == container_path),
        None,
    )
    if item is None:
        raise ApiError(404, "unknown_sound", f"{container_path!r} is not a soundboard clip")
    return channel, selection, item


@router.get("/{name}/soundboard")
async def get_soundboard(name: str, state: AppState = Authed) -> dict[str, Any]:
    _channel, selection = _clips(state, name)
    return {
        "channel": name,
        "clips": [
            {
                "name": item.host_path.stem,
                "path": _relative(state, item.host_path),
                "container_path": item.container_path,
                "origin": item.origin,
                "bytes": item.host_path.stat().st_size,
            }
            for item in selection.files
        ],
    }


@router.get("/{name}/soundboard/preview")
async def preview_sound(
    name: str,
    clip: str = Query(...),
    state: AppState = Authed,
) -> FileResponse:
    _channel, _selection, item = _selected_clip(state, name, clip)
    return FileResponse(item.host_path, filename=item.host_path.name)


@router.post("/{name}/soundboard/play", status_code=202)
async def play_sound(name: str, body: SoundboardPlayBody, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    if not (await state.supervisor.containers(name)).liquidsoap.running:
        raise ApiError(409, "channel_not_running", f"{name} has no Liquidsoap soundboard")
    channel, selection, _item = _selected_clip(state, name, body.clip)

    try:
        reply = await asyncio.to_thread(
            liqctl.push_soundboard,
            liqctl.liquidsoap_host(channel.name),
            body.clip,
            allowed=selection.container_paths,
        )
    except liqctl.LiquidsoapError as exc:
        raise ApiError(502, "liquidsoap_unreachable", str(exc)) from exc

    return {
        "accepted": True,
        "channel": name,
        "action": "soundboard_play",
        "clip": body.clip,
        "detail": reply,
    }


@router.post("/{name}/soundboard/stop", status_code=202)
async def stop_soundboard(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    if not (await state.supervisor.containers(name)).liquidsoap.running:
        raise ApiError(409, "channel_not_running", f"{name} has no Liquidsoap soundboard")

    try:
        reply = await asyncio.to_thread(
            liqctl.send,
            liqctl.liquidsoap_host(channel.name),
            liqctl.SOUNDBOARD_STOP,
        )
    except liqctl.LiquidsoapError as exc:
        raise ApiError(502, "liquidsoap_unreachable", str(exc)) from exc

    return {
        "accepted": True,
        "channel": name,
        "action": "soundboard_stop",
        "detail": reply,
    }