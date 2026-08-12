"""Shared dependencies: app state, bearer auth, and config persistence.

The token check guards a process that holds the Docker socket, so it is a
constant-time comparison and it rejects a missing token rather than treating an
unset one as "open".
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Awaitable

import yaml
from fastapi import Depends, Request

from ..config import ResolvedChannel
from ..events import CHANNEL_STATUS
from ..main import ApiError, AppState, compare_token
from ..media import atomic_write_lines, write_images_list, write_playlist
from ..models import ChannelConfig, ChannelState
from ..supervisor import write_compose

LOG = logging.getLogger("ambient.api")


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


def save_channel_config(directory: Path, config: ChannelConfig) -> Path:
    """Atomic replace: `config.yaml` is read by a live process."""
    path = Path(directory) / "config.yaml"
    body = yaml.safe_dump(
        config.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100
    )
    atomic_write_lines(path, body.splitlines())
    return path


def recompile(state: AppState, name: str, *, seed: int | None = None) -> ResolvedChannel:
    """Rewrite the generated lists and the Compose file from config.yaml."""
    channel = state.channel(name)
    rng = random.Random(seed) if seed is not None else None
    write_playlist(channel.playlist_path, channel.audio, channel.config.audio.shuffle, rng)
    write_images_list(channel.images_list_path, channel.images, channel.shuffle_images, rng)
    write_compose(state.workspace, channel)
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
