"""Channel lifecycle and the health dashboard.

Anything that changes what is on air answers `202` and emits SSE; it never
blocks on the media pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import plugins as plugin_registry
from ..config import ResolvedChannel, channel_directory
from ..events import CHANNEL_STATUS
from ..main import ApiError, AppState
from ..models import (
    CHANNEL_NAME_RE,
    MEMORY_LIMIT,
    MOUNT,
    ChannelConfig,
    ChannelState,
    Encoder,
    Health,
    Resolution,
    StrictModel,
    Visualisation,
)
from ..supervisor import ChannelBusy
from ..watchdog import Verdict, parse_progress
from .deps import Authed, recompile, save_channel_config

LOG = logging.getLogger("ambient.api.channels")

router = APIRouter(prefix="/api/channels", tags=["channels"])

DEFAULT_PLUGIN = "showfreqs-bars"


class CreateChannel(StrictModel):
    name: str
    genre: str = ""
    resolution: Resolution | None = None
    fps: int | None = None
    encoder: Encoder | None = None
    mount: str | None = None
    fallback_mount: str | None = None
    cpu_limit: float | None = None
    memory_limit: str | None = None
    rtmp_url: str = "rtmp://a.rtmp.youtube.com/live2"
    # Write-only. It is never echoed back and never logged.
    stream_key: str = ""
    visualisation: Visualisation | None = None


class PatchChannel(StrictModel):
    """Only the live-editable surface. `.env` settings need a new container."""

    genre: str | None = None
    audio: dict[str, Any] | None = None
    images: dict[str, Any] | None = None
    visualisation: dict[str, Any] | None = None
    colour: dict[str, Any] | None = None
    preset: str | None = None
    bumpers: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Status assembly
# --------------------------------------------------------------------------


def _now_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    start = text.find("{")
    if start < 0:
        return {}
    try:
        value = json.loads(text[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _pick(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if source.get(key) not in (None, ""):
            return source[key]
    return None


async def _relay_state(state: AppState, name: str) -> tuple[str, str]:
    program = await state.relay_path(name)
    preview = await state.relay_path(f"{name}/preview")
    if program is None:
        return "unknown", "unknown"
    rtmp = "connected" if program.get("ready") else "disconnected"
    hls = "ok" if (preview or {}).get("ready") else "down"
    return rtmp, hls


async def channel_status(
    state: AppState, channel: ResolvedChannel, *, detail: bool = True
) -> dict[str, Any]:
    name = channel.name
    containers = await state.supervisor.containers(name)
    verdict: Verdict | None = state.watchdog.latest.get(name)

    if containers.composer.running:
        sample = verdict.sample if verdict else None
        if sample is None:
            sample = parse_progress(await state.supervisor.read_run_file(name, "progress"))
        health = verdict.health if verdict else Health.HEALTHY
        run_state = verdict.state if verdict else ChannelState.RUNNING
    else:
        sample = None
        health = Health.DISCONNECTED
        run_state = containers.state

    now = _now_json(await state.supervisor.read_run_file(name, "now.json")) if detail else {}

    body: dict[str, Any] = {
        "name": name,
        "state": run_state.value,
        "uptime_seconds": round(sample.out_time_seconds, 1) if sample else 0.0,
        "current_track": _pick(now, "current_track", "track"),
        "next_track": _pick(now, "next_track"),
        "current_slide": _pick(now, "current_slide", "slide"),
        "visualisation": _pick(now, "visualisation", "active_plugin")
        or channel.config.visualisation.active,
        "encoder": _pick(now, "encoder") or channel.encoder.value,
        "encoder_requested": channel.encoder.value,
        "fps": sample.fps if sample else 0.0,
        "speed": sample.speed if sample else 0.0,
        "bitrate_kbps": round(sample.bitrate_kbps) if sample else 0,
        "cpu_cores": state.channel_cores(name),
        "liquidsoap_buffer": "ok" if containers.liquidsoap.running else "down",
        "rtmp": "disconnected",
        "hls": "down",
        "health": health.value,
    }
    if detail:
        body["rtmp"], body["hls"] = await _relay_state(state, name)
        body["warnings"] = channel.warnings
        body["fault"] = verdict.fault if verdict else None
        body["fault_detail"] = verdict.detail if verdict else ""
    return body


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.get("")
async def list_channels(state: AppState = Authed) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name in state.names():
        try:
            channel = state.channel(name)
        except ApiError as exc:
            errors.append({"channel": name, "detail": str(exc.detail)})
            continue
        summaries.append(await channel_status(state, channel, detail=False))
    return {"channels": summaries, "errors": errors}


@router.get("/{name}")
async def get_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name)
    body = await channel_status(state, channel)
    body["config"] = channel.config.model_dump(mode="json")
    body["resolution"] = channel.resolution.value
    body["projected_cores"] = round(channel.projected_cores, 3)
    return body


@router.post("", status_code=201)
async def create_channel(body: CreateChannel, state: AppState = Authed) -> dict[str, Any]:
    name = body.name
    if not CHANNEL_NAME_RE.match(name):
        raise ApiError(400, "invalid_channel_name", f"{name!r} must match {CHANNEL_NAME_RE.pattern}")
    directory = channel_directory(state.workspace, name)
    if directory.exists():
        raise ApiError(409, "channel_exists", f"channels/{name} already exists")
    existing = state.names()
    if len(existing) >= state.workspace.ambient.limits.max_channels:
        raise ApiError(
            409,
            "channel_limit_reached",
            f"limits.max_channels is {state.workspace.ambient.limits.max_channels}",
        )

    mount = body.mount or f"/{name}"
    fallback = body.fallback_mount or f"/{name}-fallback"
    for label, value in (("mount", mount), ("fallback_mount", fallback)):
        if not re.fullmatch(MOUNT, value):
            raise ApiError(400, "invalid_mount", f"{label} {value!r} is not a valid Icecast mount")
    if mount == fallback:
        raise ApiError(400, "invalid_mount", "mount and fallback_mount must differ")
    if body.memory_limit and not re.fullmatch(MEMORY_LIMIT, body.memory_limit):
        raise ApiError(400, "invalid_memory_limit", f"{body.memory_limit!r} is not a size")

    taken = {m for n in existing for m in _mounts(state, n)}
    if mount in taken or fallback in taken:
        raise ApiError(409, "mount_in_use", "another channel already uses that Icecast mount")

    visualisation = body.visualisation or Visualisation(
        active=_default_plugin(state), hot_set=[_default_plugin(state)]
    )
    config = ChannelConfig(name=name, genre=body.genre, visualisation=visualisation)

    for sub in ("audio", "images", "bumpers", "profiles"):
        (directory / sub).mkdir(parents=True, exist_ok=True)
    save_channel_config(directory, config)
    _write_channel_env(directory, body, mount, fallback)

    await state.events.publish(CHANNEL_STATUS, {"state": ChannelState.STOPPED.value}, channel=name)
    return {
        "name": name,
        "directory": str(directory),
        "state": ChannelState.STOPPED.value,
        "next": "add audio to channels/%s/audio, then POST /api/channels/%s/start" % (name, name),
    }


@router.patch("/{name}")
async def patch_channel(name: str, body: PatchChannel, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    data = channel.config.model_dump(mode="json")
    changed: list[str] = []
    for key, value in body.model_dump(exclude_none=True).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
        changed.append(key)
    if not changed:
        raise ApiError(400, "empty_patch", "no live-editable field was supplied")

    config = ChannelConfig.model_validate(data)
    if config.name != name:
        raise ApiError(400, "immutable_field", "a channel cannot be renamed")
    save_channel_config(channel.directory, config)
    recompile(state, name)
    reloaded = state.channel(name)
    await state.events.publish(
        CHANNEL_STATUS, {"state": ChannelState.RUNNING.value, "changed": changed}, channel=name
    )
    return {"name": name, "changed": changed, "warnings": reloaded.warnings}


@router.delete("/{name}")
async def delete_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    state.channel(name, resolve_media=False)
    try:
        directory = await state.supervisor.delete(name)
    except ChannelBusy as exc:
        raise ApiError(409, "channel_running", str(exc)) from exc
    state.watchdog.forget(name)
    state.scheduler.forget(name)
    return {"name": name, "deleted": str(directory)}


@router.post("/{name}/start", status_code=202)
async def start_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name)
    recompile(state, name)
    _guard_capacity(state, channel)
    asyncio.create_task(_run(state, name, state.supervisor.start(name), "start"))
    return {"accepted": True, "channel": name, "action": "start"}


@router.post("/{name}/stop", status_code=202)
async def stop_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    state.channel(name, resolve_media=False)
    asyncio.create_task(_run(state, name, state.supervisor.stop(name), "stop"))
    return {"accepted": True, "channel": name, "action": "stop"}


@router.post("/{name}/restart", status_code=202)
async def restart_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    state.channel(name)
    recompile(state, name)
    asyncio.create_task(_run(state, name, state.supervisor.restart(name), "restart"))
    return {"accepted": True, "channel": name, "action": "restart", "mode": "make-before-break"}


@router.get("/{name}/preview")
async def preview_url(name: str, state: AppState = Authed) -> dict[str, Any]:
    """Where the operator's player should point. The relay publishes no host port."""
    state.channel(name, resolve_media=False)
    base = state.workspace.ambient.relay.hls.rstrip("/")
    return {"channel": name, "hls": f"{base}/{name}/preview/index.m3u8"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _default_plugin(state: AppState) -> str:
    registry = plugin_registry.load_registry(state.workspace.plugins_dir)
    if DEFAULT_PLUGIN in registry:
        return DEFAULT_PLUGIN
    return next(iter(registry), DEFAULT_PLUGIN)


def _mounts(state: AppState, name: str) -> list[str]:
    try:
        channel = state.channel(name, resolve_media=False)
    except ApiError:
        return []
    return [channel.env.mount, channel.env.fallback_mount]


def _guard_capacity(state: AppState, channel: ResolvedChannel) -> None:
    cores = float(os.cpu_count() or 1)
    reserved = state.workspace.ambient.limits.reserved_cores
    running = sum(
        value
        for key, value in state.cpu.items()
        if not key.startswith(f"{channel.name}-")
    )
    if channel.projected_cores and running + channel.projected_cores > cores - reserved:
        raise ApiError(
            409,
            "insufficient_capacity",
            f"{channel.name} projects {channel.projected_cores:.2f} cores on top of "
            f"{running:.2f} in use; {cores:.0f} cores minus {reserved:.2f} reserved",
        )


def _write_channel_env(directory: Path, body: CreateChannel, mount: str, fallback: str) -> Path:
    """0600, and the key is never logged or echoed back."""
    lines = [
        "# Generated by the control plane. Contains a credential — treat it like a password.",
        f"YOUTUBE_STREAM_KEY={body.stream_key}",
        f"YOUTUBE_RTMP_URL={body.rtmp_url}",
        f"CHANNEL_RESOLUTION={body.resolution.value if body.resolution else ''}",
        f"CHANNEL_FPS={body.fps or ''}",
        f"CHANNEL_ENCODER={body.encoder.value if body.encoder else ''}",
        f"CHANNEL_CPU_LIMIT={body.cpu_limit or ''}",
        f"CHANNEL_MEMORY_LIMIT={body.memory_limit or ''}",
        f"CHANNEL_MOUNT={mount}",
        f"CHANNEL_FALLBACK_MOUNT={fallback}",
    ]
    path = directory / ".env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


async def _run(state: AppState, name: str, awaitable, action: str) -> None:
    """202 means the work happens here; failures surface as SSE, not a status code."""
    try:
        await awaitable
    except Exception as exc:
        LOG.warning("channel %s: %s failed: %s", name, action, exc)
        await state.events.publish(
            CHANNEL_STATUS,
            {"state": ChannelState.FAILED.value, "action": action, "detail": str(exc)},
            channel=name,
        )
