"""Media library, per-channel ordered selections, and operator upload.

`PUT` replaces the whole ordered list rather than patching it: reordering is
the common operation, and a positional patch API for a drag-and-drop UI invites
lost-update races between two open browsers.

Every path is validated against the two-tree rule before it is written — these
paths arrive over HTTP, so `../` traversal would otherwise mount arbitrary host
files into a stream. Upload is the same boundary from the other direction and
lives in `ambient.uploads`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .. import colorprofile
from ..config import ConfigError, channel_directory
from ..events import MEDIA_UPLOADED
from ..main import ApiError, AppState
from ..media import (
    MediaError,
    MediaKind,
    MediaRoots,
    resolve_selection,
)
from ..models import ChannelConfig, ImageOrder, StrictModel
from ..uploads import (
    COMMON_TARGET,
    KIND_ALIASES,
    KIND_BY_SEGMENT,
    STAGING_DIRNAME,
    UploadAborted,
    UploadLimits,
    UploadReceiver,
    UploadTarget,
    receive,
    relative_dir,
)
from .deps import Authed, recompile, save_channel_config, write_list

router = APIRouter(prefix="/api", tags=["media"])

LOG = logging.getLogger("ambient.api")

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
    """Extract missing color profiles. A profile describes the image, so a
    shared image is analyzed once and every channel sees it."""
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


def _upload_target(state: AppState, target: str, kind: MediaKind) -> UploadTarget:
    """`common` or one existing channel. A name from a request is hostile input."""
    if target == COMMON_TARGET:
        return UploadTarget(
            name=COMMON_TARGET,
            kind=kind,
            tree_root=state.workspace.common_dir.resolve(),
            channel=None,
        )
    try:
        directory = channel_directory(state.workspace, target)
    except ConfigError as exc:
        raise ApiError(400, "invalid_channel_name", str(exc)) from exc
    if target not in state.names():
        raise ApiError(404, "unknown_channel", f"no channel named {target!r}")
    return UploadTarget(name=target, kind=kind, tree_root=directory, channel=target)


def _field_kind(value: str, fallback: MediaKind | None) -> MediaKind:
    if not value:
        if fallback is None:
            raise UploadAborted("missing_kind", "the request carries no 'kind' field", 400)
        return fallback
    kind = KIND_ALIASES.get(value.lower())
    if kind is None:
        raise UploadAborted(
            "invalid_kind", f"kind must be audio or images, not {value[:40]!r}", 400
        )
    return kind


def _field_target(fields: dict[str, str], fallback: str | None) -> str:
    """`destination=common` or `destination=channel` plus `channel=<name>`.

    The channel is only honoured for `destination=channel`, so a request that
    names both cannot have the stray field decide where the bytes land.
    """
    destination = fields.get("destination", "").lower()
    channel = fields.get("channel", "")
    if not destination:
        if channel:
            return channel
        if fallback is None:
            raise UploadAborted(
                "missing_destination", "the request carries no 'destination' field", 400
            )
        return fallback
    if destination == COMMON_TARGET:
        return COMMON_TARGET
    if destination == "channel":
        if not channel:
            raise UploadAborted(
                "missing_channel", "destination=channel needs a 'channel' field", 400
            )
        return channel
    raise UploadAborted(
        "invalid_destination",
        f"destination must be common or channel, not {destination[:40]!r}",
        400,
    )


def _resolver(
    state: AppState, *, kind: MediaKind | None, target: str | None
) -> Callable[[dict[str, str]], UploadTarget]:
    """Turn the trailing form fields into a validated target."""

    def resolve(fields: dict[str, str]) -> UploadTarget:
        chosen = _field_kind(fields.get("kind", ""), kind)
        try:
            return _upload_target(state, _field_target(fields, target), chosen)
        except ApiError as exc:
            raise UploadAborted(exc.error, str(exc.detail), exc.status_code) from exc

    return resolve


async def _finalize(receiver: UploadReceiver, part: Any) -> None:
    """Probing and profile extraction are CPU-bound; keep the loop free for SSE."""
    await asyncio.to_thread(receiver.finish, part)


def _refused(exc: UploadAborted) -> ApiError:
    return ApiError(exc.status_code, exc.error, exc.detail)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _recompile_uploaded(state: AppState, upload: UploadTarget) -> list[str]:
    """Rewrite the generated list of every channel this upload changes.

    A watched directory is the contract's own rule for "picked up live": folder
    mode and globs are watched, an explicit list is pinned and is never rewritten
    underneath the operator — not even by an upload to the shared library, which
    is why a `common/` upload has to be considered against every channel rather
    than one. The write is an atomic replace of a file Liquidsoap is watching, so
    it lands at the next track boundary with nothing restarted.
    """
    candidates = [upload.channel] if upload.channel else state.names()
    touched: list[str] = []
    for name in candidates:
        try:
            channel = state.channel(name)
        except ApiError as exc:
            LOG.warning("upload: channel %s not recompiled: %s", name, exc.detail)
            continue
        selection = channel.audio if upload.kind is MediaKind.AUDIO else channel.images
        if not any(_within(upload.directory, w) for w in selection.watched_dirs):
            continue
        try:
            write_list(channel, upload.kind)
        except OSError as exc:
            LOG.warning("upload: channel %s not recompiled: %s", name, exc)
            continue
        touched.append(name)
    return touched


async def _upload(
    request: Request,
    state: AppState,
    *,
    kind: MediaKind | None,
    target: str | None,
    fallback: str | None,
    on_conflict: str,
) -> JSONResponse:
    """Stream a multipart batch into `common/` or one channel's own tree.

    An explicit `?target=` is resolved before a byte is read, so a duplicate or
    a bad extension is refused without receiving the file. Otherwise the target
    comes from the body's own fields, which the browser appends *after* the
    file, and the upload stages until they arrive.

    Per-file results, so one rejected file never fails the batch: 200 when all
    succeeded, 207 when some did, 400 when none did.
    """
    limits = UploadLimits.from_config(state.workspace.ambient)
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > limits.max_request_bytes:
        raise ApiError(
            413, "request_too_large", f"at most {limits.max_request_bytes} bytes per request"
        )

    eager = _upload_target(state, target, kind) if target is not None and kind else None
    receiver = UploadReceiver(
        eager,
        limits,
        rename_on_conflict=on_conflict == "rename",
        repo_root=state.workspace.root,
        resolve_target=None if eager else _resolver(state, kind=kind, target=fallback),
        staging_dir=None if eager else state.workspace.root / STAGING_DIRNAME,
    )
    try:
        results = await receive(
            request.stream(),
            request.headers.get("content-type") or "",
            receiver,
            finalize=_finalize,
        )
    except UploadAborted as exc:
        raise _refused(exc) from exc

    if not results:
        raise ApiError(400, "no_files", "the request carried no file parts")

    # Every file may have failed before the target was ever needed.
    try:
        upload = receiver.resolve()
    except UploadAborted as exc:
        raise _refused(exc) from exc

    stored = [r for r in results if r.ok]
    # A rejected batch stored nothing, so rewriting a watched list would be churn.
    recompiled = (
        await asyncio.to_thread(_recompile_uploaded, state, upload) if stored else []
    )
    body = {
        "target": upload.name,
        "destination": COMMON_TARGET if upload.channel is None else "channel",
        "channel": upload.channel,
        "kind": upload.kind.folder,
        "directory": relative_dir(upload.directory, state.workspace.root),
        "uploaded": len(stored),
        "failed": len(results) - len(stored),
        "recompiled": recompiled,
        "results": [r.as_dict() for r in results],
    }
    if stored:
        await state.events.publish(
            MEDIA_UPLOADED,
            {
                "target": upload.name,
                "kind": upload.kind.folder,
                "uploaded": len(stored),
                "failed": len(results) - len(stored),
                "files": [r.filename for r in stored],
            },
            channel=upload.channel,
        )
    status = 200 if len(stored) == len(results) else (207 if stored else 400)
    return JSONResponse(status_code=status, content=body)


@router.post("/media/upload")
async def upload_media(
    request: Request,
    on_conflict: Literal["reject", "rename"] = "reject",
    state: AppState = Authed,
) -> JSONResponse:
    """Upload with everything in the body: `files`, `kind`, `destination`, `channel`.

    Nothing is implied here — the route names neither the kind nor the tree, so
    a request missing either field is refused rather than defaulted into the
    shared library.
    """
    return await _upload(
        request, state, kind=None, target=None, fallback=None, on_conflict=on_conflict
    )


@router.post("/media/{kind}/upload")
async def upload_media_kind(
    kind: Literal["audio", "images"],
    request: Request,
    request_target: str | None = Query(None, alias="target"),
    on_conflict: Literal["reject", "rename"] = "reject",
    state: AppState = Authed,
) -> JSONResponse:
    """The same upload with the kind in the path.

    `?target=` still wins when it is given; without it the body's `destination`
    decides, and a request that says nothing at all keeps the historical
    `common` default.
    """
    return await _upload(
        request,
        state,
        kind=KIND_BY_SEGMENT[kind],
        target=request_target,
        fallback=COMMON_TARGET,
        on_conflict=on_conflict,
    )


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
    """Normalize, prefix-check, resolve symlinks, prefix-check again."""
    roots = MediaRoots.create(state.workspace.root, state.workspace.common_dir, directory)
    try:
        resolve_selection(entries, kind, roots)
    except MediaError as exc:
        raise ApiError(400, "invalid_media_path", str(exc)) from exc
