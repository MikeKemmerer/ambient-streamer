"""System: health, capacity, logs, SSE and metrics.

`GET /api/health` is the only unauthenticated endpoint. `GET /api/system`
reports probe results rather than the encoder list — an encoder present in
`ffmpeg -encoders` whose device is missing is reported unavailable. The probe
runs a real test encode inside the composer image, which is where encoding
happens and the only image in the stack that ships ffmpeg.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import PlainTextResponse, StreamingResponse

from .. import metrics as metrics_module
from ..config import ConfigError, ignored_channel_names
from ..main import ApiError, AppState
from ..supervisor import LOG_SERVICES
from .deps import Authed, get_state

router = APIRouter(prefix="/api", tags=["system"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Connection": "keep-alive",
    # nginx buffers text/event-stream by default, which delays every event.
    "X-Accel-Buffering": "no",
}


@router.get("/health")
async def health(state: AppState = Depends(get_state)) -> dict[str, Any]:
    """Unauthenticated liveness. Reports nothing an anonymous caller should not see."""
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - state.started_at, 1),
        "channels": len(state.names()),
        "watchdog": state.watchdog.enabled,
    }


@router.get("/system")
async def system(
    refresh: bool = Query(False, description="re-run the encoder probes"),
    state: AppState = Authed,
) -> dict[str, Any]:
    ambient = state.workspace.ambient
    probes = await state.supervisor.probe_encoders(
        [encoder.value for encoder in ambient.encoders.probe_order], refresh=refresh
    )
    cores = float(os.cpu_count() or 1)
    return {
        "cores": cores,
        "memory_bytes": _memory_bytes(),
        "reserved_cores": ambient.limits.reserved_cores,
        "max_channels": ambient.limits.max_channels,
        "encoders": [
            {"encoder": p.encoder, "available": p.available, "detail": p.detail} for p in probes
        ],
        "encoder_probe_image": state.supervisor.composer_image,
        "fallback_encoder": ambient.encoders.fallback.value,
        "bind_address": state.bind_address,
        "authenticated": bool(state.token),
        "ignored_channels": ignored_channel_names(state.workspace),
        "log_services": sorted(LOG_SERVICES),
        "paths": {
            "root": str(state.workspace.root),
            "common": str(state.workspace.common_dir),
            "channels": str(state.workspace.channels_dir),
            "logs": str(state.workspace.log_dir),
        },
        "warnings": state.workspace.warnings,
    }


@router.get("/capacity")
async def capacity(state: AppState = Authed) -> dict[str, Any]:
    cores = float(os.cpu_count() or 1)
    reserved = state.workspace.ambient.limits.reserved_cores
    channels: list[dict[str, Any]] = []
    projected = 0.0
    for name in state.names():
        try:
            channel = state.channel(name, resolve_media=False)
        except ApiError:
            continue
        running = state.watchdog.latest.get(name)
        channels.append(
            {
                "channel": name,
                "projected_cores": round(channel.projected_cores, 3),
                "cores_breakdown": channel.cores_breakdown,
                "measured_cores": state.channel_cores(name),
                "state": running.state.value if running else "stopped",
            }
        )
        projected += channel.projected_cores
    available = cores - reserved
    body = {
        "cores": cores,
        "reserved_cores": reserved,
        "available_cores": round(available, 3),
        "projected_cores": round(projected, 3),
        "measured_cores": round(sum(state.cpu.values()), 3),
        "headroom_cores": round(available - projected, 3),
        "channels": channels,
    }
    return body


@router.get("/logs")
async def logs(
    channel: str = Query(...),
    service: str = Query("compositor"),
    lines: int = Query(200, ge=1, le=2000),
    state: AppState = Authed,
) -> dict[str, Any]:
    """`text` is one plain-text blob, not an array: the UI shows it in a textarea."""
    state.channel(channel, resolve_media=False)
    try:
        tail = await state.supervisor.read_log(channel, service, lines)
    except ConfigError as exc:
        raise ApiError(400, "invalid_log_service", str(exc)) from exc
    return {
        "channel": channel,
        "service": service,
        "services": sorted(LOG_SERVICES),
        "text": tail.text,
        "line_count": tail.line_count,
        "source": tail.source,
        "container": tail.container,
        "path": str(tail.path),
        "detail": tail.detail,
    }


@router.get("/events")
async def events(request: Request, state: AppState = Authed) -> StreamingResponse:
    """Advisory only. A reconnecting client re-reads `GET /api/channels`."""
    hub = state.events

    async def frames():
        async with hub.subscribe() as subscriber:
            async for frame in hub.stream(subscriber):
                if await request.is_disconnected():
                    return
                yield frame

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/metrics")
async def metrics(state: AppState = Authed) -> PlainTextResponse:
    return PlainTextResponse(
        metrics_module.render(state.watchdog, cpu=state.cpu),
        media_type=metrics_module.CONTENT_TYPE,
    )


def _memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform dependent
        return 0
