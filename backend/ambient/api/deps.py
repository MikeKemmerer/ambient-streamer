"""Shared dependencies: app state, bearer auth, and config persistence.

The token check guards a process that holds the Docker socket, so it is a
constant-time comparison and it rejects a missing token rather than treating an
unset one as "open".
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import re
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable

import yaml
from fastapi import Depends, Request

from ..config import ResolvedChannel
from ..events import CHANNEL_STATUS, CHANNEL_VISUALIZATION
from ..main import ApiError, AppState, compare_token
from ..media import MediaKind, atomic_write_lines, write_images_list, write_playlist
from ..models import ChannelConfig, ChannelState
from ..supervisor import write_compose

LOG = logging.getLogger("ambient.api")


def parse_now_json(text: str) -> dict[str, Any]:
    """The composer's status file, read through `docker exec` and so prefixed."""
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


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "ambient", None)
    if state is None:  # pragma: no cover - only before lifespan runs
        raise ApiError(503, "not_ready", "the control plane is still starting")
    return state


def require_token(request: Request, state: AppState = Depends(get_state)) -> AppState:
    scheme, _, supplied = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not compare_token(supplied.strip(), state.token):
        raise ApiError(401, "unauthorized", "a valid bearer token is required")
    return state


Authed = Depends(require_token)


async def serialize_visualization_mutation(
    name: str, state: AppState = Authed
) -> AsyncIterator[AppState]:
    async with state.visualization_lock(name):
        yield state


VisualMutation = Depends(serialize_visualization_mutation)


def save_channel_config(directory: Path, config: ChannelConfig) -> Path:
    """Atomic replace: `config.yaml` is read by a live process."""
    path = Path(directory) / "config.yaml"
    body = yaml.safe_dump(
        config.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100
    )
    atomic_write_lines(path, body.splitlines())
    return path


def write_list(
    channel: ResolvedChannel, kind: MediaKind, rng: random.Random | None = None
) -> list[str]:
    """Rewrite one generated list. Atomic, because a live process reads it."""
    if kind is MediaKind.AUDIO:
        return write_playlist(
            channel.playlist_path, channel.audio, channel.config.audio.shuffle, rng
        )
    return write_images_list(
        channel.images_list_path, channel.images, channel.shuffle_images, rng
    )


def recompile(
    state: AppState,
    name: str,
    *,
    seed: int | None = None,
    preserve_visualizer_accent: bool = False,
) -> ResolvedChannel:
    """Rewrite the generated lists and the Compose file from config.yaml."""
    channel = state.channel(name)
    rng = random.Random(seed) if seed is not None else None
    write_list(channel, MediaKind.AUDIO, rng)
    write_list(channel, MediaKind.IMAGE, rng)
    compose_channel = channel
    if preserve_visualizer_accent:
        accent_path = state.workspace.run_dir / name / "viz-accent"
        try:
            accent = accent_path.read_text(encoding="utf-8").strip()
        except OSError:
            accent = ""
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
            try:
                document = yaml.safe_load(channel.compose_path.read_text(encoding="utf-8"))
                accent = str(
                    document["services"][f"{name}-visualizer"]
                    .get("environment", {})
                    .get("ACCENT", "")
                ).strip()
            except (KeyError, OSError, TypeError, yaml.YAMLError):
                accent = ""
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
            data = channel.config.model_dump(mode="json")
            data["color"]["manual"]["accent"] = accent
            compose_channel = dataclasses.replace(
                channel, config=ChannelConfig.model_validate(data)
            )
    write_compose(state.workspace, compose_channel)
    return channel


async def run_action(state: AppState, name: str, awaitable: Awaitable, action: str) -> None:
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


async def publish_visualization_event(
    state: AppState,
    name: str,
    *,
    event_state: str,
    generation: int | None = None,
    applied: bool,
    error: str | None = None,
    detail: str = "",
    **data: Any,
) -> None:
    await state.events.publish(
        CHANNEL_VISUALIZATION,
        {
            **data,
            "generation": generation,
            "applied": applied,
            "error": error,
            "detail": detail,
            "state": event_state,
        },
        channel=name,
    )


def queue_visualizer_action(
    state: AppState,
    name: str,
    action: str,
    *,
    event_data: dict[str, Any] | None = None,
) -> int:
    generation = state.supervisor.next_visualizer_generation(name)
    method = getattr(state.supervisor, f"{action}_visualizer")
    asyncio.create_task(
        run_visualizer_action(
            state,
            name,
            method(name, generation=generation),
            action,
            generation,
            event_data=event_data,
        )
    )
    return generation


async def run_visualizer_action(
    state: AppState,
    name: str,
    awaitable: Awaitable,
    action: str,
    generation: int,
    *,
    event_data: dict[str, Any] | None = None,
) -> None:
    context = dict(event_data or {})
    await state.events.publish(
        CHANNEL_VISUALIZATION,
        {
            **context,
            "action": action,
            "generation": generation,
            "applied": False,
            "error": None,
            "detail": "visualizer action queued",
            "state": "queued",
        },
        channel=name,
    )
    try:
        result = await awaitable
        await state.events.publish(
            CHANNEL_VISUALIZATION,
            {
                **context,
                "action": action,
                "generation": result.generation,
                "applied": result.applied,
                "error": None,
                "detail": (
                    "visualizer action applied"
                    if result.applied
                    else "superseded by a newer visualizer request"
                ),
                "state": "applied" if result.applied else "superseded",
            },
            channel=name,
        )
    except Exception as exc:
        LOG.warning("channel %s: visualizer %s failed: %s", name, action, exc)
        await state.events.publish(
            CHANNEL_VISUALIZATION,
            {
                **context,
                "action": action,
                "generation": generation,
                "applied": False,
                "error": "visualizer_action_failed",
                "detail": str(exc),
                "state": "failed",
            },
            channel=name,
        )
