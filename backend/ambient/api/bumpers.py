"""Bumpers: source text, intervals and generated audio.

Generation runs as a one-shot container owned by the media-pipeline lane; this
router owns `bumpers.yaml`, the interval settings and the job's progress
events. The TTS model is never part of the 24/7 footprint.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter
from pydantic import Field, ValidationError, field_validator
from starlette.responses import FileResponse

from ..events import JOB_PROGRESS
from ..main import ApiError, AppState
from ..media import atomic_write_lines
from ..models import BumperMode, ChannelConfig, StrictModel
from .deps import Authed, recompile, save_channel_config

LOG = logging.getLogger("ambient.api.bumpers")

router = APIRouter(prefix="/api/channels", tags=["bumpers"])

BUMPER_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
GENERATED_SUFFIX = ".mp3"


class BumperText(StrictModel):
    id: str
    text: str

    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        if not BUMPER_ID.match(value):
            raise ValueError(f"bumper id {value!r} must match {BUMPER_ID.pattern}")
        return value


class BumperSource(StrictModel):
    version: Literal[1] = 1
    voice: str = "af_heart"
    speed: float = Field(1.0, gt=0, le=3.0)
    bed: str | None = None
    bed_gain_db: float = -18.0
    lead_in_seconds: float = Field(1.5, ge=0)
    lead_out_seconds: float = Field(2.5, ge=0)
    bumpers: list[BumperText] = Field(default_factory=list)


class BumpersBody(StrictModel):
    enabled: bool | None = None
    mode: BumperMode | None = None
    every_tracks: int | None = Field(None, ge=1)
    every_minutes: int | None = Field(None, ge=1)
    sources: list[str] | None = None
    text: BumperSource | None = None


def _source_path(directory: Path) -> Path:
    return directory / "bumpers.yaml"


def _read_source(directory: Path) -> BumperSource:
    path = _source_path(directory)
    if not path.is_file():
        return BumperSource()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return BumperSource.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ApiError(400, "invalid_bumpers_yaml", f"{path}: {exc}") from exc


def _write_source(directory: Path, source: BumperSource) -> None:
    body = yaml.safe_dump(source.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    atomic_write_lines(_source_path(directory), body.splitlines())


def _generated(directory: Path) -> list[dict[str, Any]]:
    folder = directory / "bumpers"
    if not folder.is_dir():
        return []
    return [
        {"id": path.stem, "file": path.name, "bytes": path.stat().st_size}
        for path in sorted(folder.glob(f"*{GENERATED_SUFFIX}"))
        if path.is_file()
    ]


@router.get("/{name}/bumpers")
async def get_bumpers(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    source = _read_source(channel.directory)
    return {
        "channel": name,
        "insertion": channel.config.bumpers.model_dump(mode="json"),
        "text": source.model_dump(mode="json"),
        "generated": _generated(channel.directory),
    }


@router.put("/{name}/bumpers")
async def put_bumpers(name: str, body: BumpersBody, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    data = channel.config.model_dump(mode="json")
    insertion = data["bumpers"]
    for key in ("enabled", "mode", "every_tracks", "every_minutes", "sources"):
        value = getattr(body, key)
        if value is not None:
            insertion[key] = value.value if isinstance(value, BumperMode) else value

    config = ChannelConfig.model_validate(data)
    save_channel_config(channel.directory, config)
    if body.text is not None:
        _write_source(channel.directory, body.text)
    # Bumpers are audio, and audio lives behind Icecast: no compositor restart.
    recompile(state, name)
    return {
        "channel": name,
        "insertion": config.bumpers.model_dump(mode="json"),
        "text": _read_source(channel.directory).model_dump(mode="json"),
    }


@router.post("/{name}/bumpers/generate", status_code=202)
async def generate_bumpers(name: str, state: AppState = Authed) -> dict[str, Any]:
    channel = state.channel(name, resolve_media=False)
    source = _read_source(channel.directory)
    if not source.bumpers:
        raise ApiError(400, "no_bumper_text", f"{channel.directory}/bumpers.yaml lists no bumpers")
    asyncio.create_task(_report_job(state, name, [b.id for b in source.bumpers]))
    return {
        "accepted": True,
        "channel": name,
        "bumpers": [b.id for b in source.bumpers],
        "job": f"bumpers:{name}",
    }


@router.get("/{name}/bumpers/{bumper_id}/preview")
async def preview_bumper(name: str, bumper_id: str, state: AppState = Authed) -> FileResponse:
    channel = state.channel(name, resolve_media=False)
    if not BUMPER_ID.match(bumper_id):
        raise ApiError(400, "invalid_bumper_id", f"{bumper_id!r} is not a bumper id")
    path = channel.directory / "bumpers" / f"{bumper_id}{GENERATED_SUFFIX}"
    resolved = path.resolve()
    if not resolved.is_file() or resolved.parent != (channel.directory / "bumpers").resolve():
        raise ApiError(404, "unknown_bumper", f"{bumper_id!r} has not been generated")
    return FileResponse(resolved, media_type="audio/mpeg", filename=resolved.name)


async def _report_job(state: AppState, name: str, ids: list[str]) -> None:
    """The synthesis container belongs to media-pipeline; this reports its shape."""
    await state.events.publish(
        JOB_PROGRESS,
        {
            "job": f"bumpers:{name}",
            "status": "queued",
            "bumpers": ids,
            "detail": "synthesis runs as a one-shot container",
        },
        channel=name,
    )
