"""Render a channel's Compose file from the infra lane's frozen template.

`docker/compose.channel.yml.j2` is owned by the infra lane and is read, never
written. Phase 1 renders it into `channels/<name>/docker-compose.yml`; the
start/stop/restart half of the supervisor arrives with the REST layer in
Phase 3.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from .config import ResolvedChannel, Workspace
from .media import atomic_write_lines

TEMPLATE_DIR = "docker"
TEMPLATE_NAME = "compose.channel.yml.j2"


class ComposeError(ValueError):
    """The Compose file could not be rendered, or did not render to valid YAML."""


def compose_template_path(workspace: Workspace) -> Path:
    return workspace.root / TEMPLATE_DIR / TEMPLATE_NAME


def compose_context(workspace: Workspace, channel: ResolvedChannel) -> dict[str, str]:
    """The variables the template declares. `image_tag` is left to its default."""
    return {
        "channel": channel.name,
        "repo_root": str(workspace.root),
        "common_dir": str(workspace.common_dir),
        "channel_dir": str(channel.directory),
        # Per-channel, because both containers mount it at /var/log/ambient.
        "log_dir": str(workspace.log_dir / channel.name),
        "encoder": channel.encoder.value,
        # channel.liq reads CROSSFADE_SECONDS; without this the value resolved
        # from config.yaml never reaches it and the script default silently wins.
        "crossfade_seconds": str(channel.crossfade_seconds),
    }


def render_compose(workspace: Workspace, channel: ResolvedChannel) -> str:
    """Render the template and prove the result is a usable Compose document."""
    template_path = compose_template_path(workspace)
    if not template_path.is_file():
        raise ComposeError(f"{template_path} is missing; cannot render a Compose file")

    # No autoescape: this is YAML, and HTML escaping would corrupt host paths.
    environment = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
    )
    try:
        rendered = environment.get_template(TEMPLATE_NAME).render(
            compose_context(workspace, channel)
        )
    except TemplateError as exc:
        raise ComposeError(f"{template_path}: {exc}") from exc

    # A template typo must fail here, not later as an opaque `docker compose` error.
    try:
        document = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise ComposeError(
            f"{template_path} rendered invalid YAML for channel {channel.name!r}: {exc}"
        ) from exc
    if not isinstance(document, dict) or not document.get("services"):
        raise ComposeError(
            f"{template_path} rendered no services for channel {channel.name!r}"
        )
    return rendered


def write_compose(workspace: Workspace, channel: ResolvedChannel) -> Path:
    """Write `channels/<name>/docker-compose.yml`. A half-written file is a dead channel."""
    rendered = render_compose(workspace, channel)
    atomic_write_lines(channel.compose_path, rendered.splitlines())
    return channel.compose_path
