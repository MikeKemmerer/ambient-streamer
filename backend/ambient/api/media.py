"""Media library and per-channel ordered selections.

`PUT` replaces the whole ordered list rather than patching it: reordering is
the common operation, and a positional patch API for a drag-and-drop UI invites
lost-update races between two open browsers.

Every path is validated against the two-tree rule before it is written — these
paths arrive over HTTP, so `../` traversal would otherwise mount arbitrary host
files into a stream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import Field

from .. import colorprofile
from ..config import channel_directory
from ..main import ApiError, AppState
from ..media import (
    MediaError,
    MediaKind,
    MediaRoots,
    resolve_selection,
)
from ..models import ChannelConfig, ImageOrder, StrictModel
from .deps import Authed, recompile, save_channel_config

router = APIRouter(prefix="/api", tags=["media"])

MAX_ENTRIES = 5000


class PlaylistBody(StrictModel):
    tracks: list[str] = Field(default_factory=list)
    shuffle: bool | None = None
    crossfade_seconds: float | None = Field(None, ge=0)


class ImagesBody(StrictModel):
    slides: list[str] = Field(default_factory=list)
    order: ImageOrder | None = None
    hold_seconds: float | None = Field(None, gt=0)
    fade_seconds: float | None = Field(None, ge=0)


def _entries(values: list[str]) -> list[str]:
    if len(values) > MAX_ENTRIES:
        raise ApiError(400, "selection_too_large", f"at most {MAX_ENTRIES} entries")
    return values


def _relative(state: AppState, path: Path) -> str:
    try:
        return str(path.relative_to(state.workspace.root).as_posix())
    except ValueError:
        return str(path)


def _listing(state: AppState, tree: Path, kind: MediaKind, *, profiles: bool) -> list[dict]:
    folder = tree / kind.folder
    if not folder.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if path.name.startswith(".") or path.suffix.lower() not in kind.extensions:
            continue
        entry: dict[str, Any] = {
            "path": _relative(state, path),
            "name": path.name,
            "bytes": path.stat().st_size,
        }
        if profiles:
            entry["profile"] = colorprofile.summarise(
                colorprofile.read_profile(colorprofile.profile_path(path, tree))
            )
        items.append(entry)
    return items


def _library(state: AppState, kind: MediaKind, *, profiles: bool = False) -> dict[str, Any]:
    channels: dict[str, list[dict]] = {}
    for name in state.names():
        directory = channel_directory(state.workspace, name)
        channels[name] = _listing(state, directory, kind, profiles=profiles)
    return {
        "common": _listing(state, state.workspace.common_dir, kind, profiles=profiles),
        "channels": channels,
    }


@router.get("/media/audio")
async def list_audio(state: AppState = Authed) -> dict[str, Any]:
    return _library(state, MediaKind.AUDIO)


@router.get("/media/images")
async def list_images(state: AppState = Authed) -> dict[str, Any]:
    return _library(state, MediaKind.IMAGE, profiles=True)


@router.post("/media/profiles", status_code=202)
async def extract_profiles(
    force: bool = False, channel: str | None = None, state: AppState = Authed
) -> dict[str, Any]:
    """Extract missing colour profiles. A profile describes the image, so a
    shared image is analysed once and every channel sees it."""
    trees: list[Path] = []
    if channel is None:
        trees.append(state.workspace.common_dir)
        trees.extend(channel_directory(state.workspace, n) for n in state.names())
    else:
        state.channel(channel, resolve_media=False)
        trees.append(channel_directory(state.workspace, channel))
    results = [
        colorprofile.refresh_tree(tree, repo_root=state.workspace.root, force=force)
        for tree in trees
    ]
    return {"accepted": True, "trees": results}


@router.get("/channels/{name}/playlist")
async def get_playlist(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name)
    return {
        "channel": name,
        "tracks": channel.config.audio.tracks,
        "shuffle": channel.config.audio.shuffle,
        "crossfade_seconds": channel.crossfade_seconds,
        "resolved": [_relative(state, f.host_path) for f in channel.audio.files],
        "container_paths": channel.audio.container_paths,
        "watched": [_relative(state, p) for p in channel.audio.watched_dirs],
        "warnings": channel.audio.warnings,
    }


@router.put("/channels/{name}/playlist")
async def put_playlist(name: str, body: PlaylistBody, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    tracks = _entries(body.tracks)
    _validate(state, channel.directory, tracks, MediaKind.AUDIO)

    data = channel.config.model_dump(mode="json")
    data["audio"]["tracks"] = tracks
    if body.shuffle is not None:
        data["audio"]["shuffle"] = body.shuffle
    if body.crossfade_seconds is not None:
        data["audio"]["crossfade_seconds"] = body.crossfade_seconds
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))

    updated = recompile(state, name)
    return {
        "channel": name,
        "tracks": updated.config.audio.tracks,
        "resolved": len(updated.audio.files),
        "warnings": updated.audio.warnings,
    }


@router.get("/channels/{name}/images")
async def get_images(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name)
    return {
        "channel": name,
        "slides": channel.config.images.slides,
        "order": channel.config.images.order.value,
        "hold_seconds": channel.hold_seconds,
        "fade_seconds": channel.fade_seconds,
        "resolved": [_relative(state, f.host_path) for f in channel.images.files],
        "container_paths": channel.images.container_paths,
        "watched": [_relative(state, p) for p in channel.images.watched_dirs],
        "warnings": channel.images.warnings,
    }


@router.put("/channels/{name}/images")
async def put_images(name: str, body: ImagesBody, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    slides = _entries(body.slides)
    _validate(state, channel.directory, slides, MediaKind.IMAGE)

    data = channel.config.model_dump(mode="json")
    data["images"]["slides"] = slides
    if body.order is not None:
        data["images"]["order"] = body.order.value
    if body.hold_seconds is not None:
        data["images"]["hold_seconds"] = body.hold_seconds
    if body.fade_seconds is not None:
        data["images"]["fade_seconds"] = body.fade_seconds
    save_channel_config(channel.directory, ChannelConfig.model_validate(data))

    updated = recompile(state, name)
    return {
        "channel": name,
        "slides": updated.config.images.slides,
        "resolved": len(updated.images.files),
        "warnings": updated.images.warnings,
    }


def _validate(state: AppState, directory: Path, entries: list[str], kind: MediaKind) -> None:
    """Normalise, prefix-check, resolve symlinks, prefix-check again."""
    roots = MediaRoots.create(state.workspace.root, state.workspace.common_dir, directory)
    try:
        resolve_selection(entries, kind, roots)
    except MediaError as exc:
        raise ApiError(400, "invalid_media_path", str(exc)) from exc
