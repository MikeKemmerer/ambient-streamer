"""Channel lifecycle and the health dashboard.

Anything that changes what is on air answers `202` and emits SSE; it never
blocks on the media pipeline.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import plugins as plugin_registry
from ..config import (
    ResolvedChannel,
    channel_directory,
    ignored_channel_names,
    parse_env_file,
)
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
    Visualization,
)
from ..supervisor import ChannelBusy
from ..watchdog import Verdict, parse_progress
from .deps import Authed, parse_now_json, recompile, run_action, save_channel_config

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
    visualization: Visualization | None = None


class PatchChannel(StrictModel):
    """Only the live-editable surface. `.env` settings need a new container."""

    genre: str | None = None
    audio: dict[str, Any] | None = None
    images: dict[str, Any] | None = None
    visualization: dict[str, Any] | None = None
    color: dict[str, Any] | None = None
    preset: str | None = None
    bumpers: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None


class ResolutionBody(StrictModel):
    resolution: Resolution


# --------------------------------------------------------------------------
# Status assembly
# --------------------------------------------------------------------------


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

    now = (
        parse_now_json(await state.supervisor.read_run_file(name, "now.json"))
        if detail
        else {}
    )

    body: dict[str, Any] = {
        "name": name,
        "state": run_state.value,
        "uptime_seconds": round(sample.out_time_seconds, 1) if sample else 0.0,
        "current_track": _pick(now, "current_track", "track"),
        "next_track": _pick(now, "next_track"),
        "current_slide": _pick(now, "current_slide", "slide"),
        # From the config, not now.json: a streamselect switch deliberately does
        # not restart the composer, so now.json's copy is frozen at boot.
        "visualization": channel.config.visualization.active,
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
    return {
        "channels": summaries,
        "errors": errors,
        "ignored": ignored_channel_names(state.workspace),
    }


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

    visualization = body.visualization or Visualization(
        active=_default_plugin(state), hot_set=[_default_plugin(state)]
    )
    config = ChannelConfig(name=name, genre=body.genre, visualization=visualization)

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
    asyncio.create_task(run_action(state, name, state.supervisor.start(name), "start"))
    return {"accepted": True, "channel": name, "action": "start"}


@router.post("/{name}/stop", status_code=202)
async def stop_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    state.channel(name, resolve_media=False)
    asyncio.create_task(run_action(state, name, state.supervisor.stop(name), "stop"))
    return {"accepted": True, "channel": name, "action": "stop"}


@router.post("/{name}/restart", status_code=202)
async def restart_channel(name: str, state: AppState = Authed) -> dict[str, Any]:
    state.channel(name)
    recompile(state, name)
    asyncio.create_task(run_action(state, name, state.supervisor.restart(name), "restart"))
    return {"accepted": True, "channel": name, "action": "restart", "mode": "make-before-break"}


@router.put("/{name}/resolution", status_code=202)
async def set_resolution(
    name: str, body: ResolutionBody, state: AppState = Authed
) -> dict[str, Any]:
    """Not a live change. The filtergraph is fixed at launch, so the channel restarts."""
    channel = state.channel(name, resolve_media=False)
    previous = channel.resolution
    if body.resolution is previous:
        return {
            "accepted": False,
            "channel": name,
            "resolution": previous.value,
            "restarted": False,
            "detail": f"{name} is already {previous.value}",
        }

    restore = _raw_resolution(channel.directory)
    _write_env_values(channel.directory, {"CHANNEL_RESOLUTION": body.resolution.value})
    try:
        updated = state.channel(name)
        _guard_capacity(state, updated)
    except ApiError:
        # A refused change must not survive in .env.
        _write_env_values(channel.directory, {"CHANNEL_RESOLUTION": restore})
        raise
    recompile(state, name)

    running = (await state.supervisor.containers(name)).composer.running
    if running:
        asyncio.create_task(
            run_action(state, name, state.supervisor.restart(name), "resolution")
        )
    await state.events.publish(
        CHANNEL_STATUS,
        {"state": ChannelState.STARTING.value if running else ChannelState.STOPPED.value,
         "changed": ["resolution"]},
        channel=name,
    )
    return {
        "accepted": True,
        "channel": name,
        "resolution": body.resolution.value,
        "previous": previous.value,
        "projected_cores": round(updated.projected_cores, 3),
        "restarted": running,
        "mode": "make-before-break" if running else "applied-on-next-start",
        "detail": (
            "the filtergraph is fixed at launch, so the compositor is being replaced; "
            "measured ~1s of RTMP gap and a new YouTube ingest session"
            if running
            else "the channel is stopped; the new resolution applies on the next start"
        ),
    }


@router.get("/{name}/preview")
async def preview_url(name: str, state: AppState = Authed) -> dict[str, Any]:
    """Where the operator's player should point.

    Relative, and proxied by this process: the relay publishes no host port, and
    its internal address resolves nowhere in a browser.
    """
    state.channel(name, resolve_media=False)
    return {"channel": name, "hls": f"/{name}/preview/index.m3u8"}


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


def _raw_resolution(directory: Path) -> str:
    """The literal `.env` value, which may be empty and inherit the default."""
    return parse_env_file(Path(directory) / ".env").get("CHANNEL_RESOLUTION", "")


def _write_env_values(directory: Path, values: dict[str, str]) -> Path:
    """Rewrite named keys in place, preserving everything else including the key.

    Read-modify-write rather than a full regeneration: this file holds the
    stream key, which the control plane must not be able to lose.
    """
    path = Path(directory) / ".env"
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    out: list[str] = []
    for raw in existing:
        key = raw.split("=", 1)[0].strip().removeprefix("export ").strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(raw)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")
    return path
