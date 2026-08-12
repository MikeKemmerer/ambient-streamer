"""CLI: compile a channel's configuration into everything it needs to start.

    python -m ambient.compile <channel> [--all] [--dry-run] [--json]

Writes `playlist.m3u`, `images.list` and `docker-compose.yml` atomically. This
is what the installer and the other lanes call in Phase 1; the REST layer
arrives in Phase 3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from random import Random

from .config import (
    ConfigError,
    ResolvedChannel,
    Workspace,
    check_mount_uniqueness,
    discover_channels,
    load_channel,
    load_workspace,
)
from .media import MediaError, write_images_list, write_playlist
from .supervisor import ComposeError, render_compose, write_compose

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def _repo_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    env = os.environ.get("AMBIENT_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_ROOT


def compile_channel(
    workspace: Workspace,
    channel: ResolvedChannel,
    dry_run: bool = False,
    seed: int | None = None,
) -> dict:
    rng = Random(seed) if seed is not None else None
    playlist = channel.audio.container_paths
    slides = channel.images.container_paths
    if dry_run:
        # Rendered anyway: a dry run that hides a broken template is worthless.
        render_compose(workspace, channel)
    else:
        playlist = write_playlist(
            channel.playlist_path, channel.audio, channel.config.audio.shuffle, rng
        )
        slides = write_images_list(
            channel.images_list_path, channel.images, channel.shuffle_images, rng
        )
        write_compose(workspace, channel)
    return {
        "channel": channel.name,
        "resolution": channel.resolution.value,
        "fps": channel.fps,
        "encoder": channel.encoder.value,
        "playlist": str(channel.playlist_path),
        "images_list": str(channel.images_list_path),
        "compose": str(channel.compose_path),
        "tracks": len(playlist),
        "slides": len(slides),
        "watched_dirs": [
            str(p) for p in (channel.audio.watched_dirs + channel.images.watched_dirs)
        ],
        "warnings": channel.warnings,
        "dry_run": dry_run,
    }


def _select_channels(workspace: Workspace, args: argparse.Namespace) -> list[str]:
    if args.all:
        return discover_channels(workspace)
    return list(args.channels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ambient.compile", description=__doc__)
    parser.add_argument("channels", nargs="*", help="channel names under channels/")
    parser.add_argument("--all", action="store_true", help="compile every configured channel")
    parser.add_argument("--repo-root", default=None, help="repository root (default: autodetect)")
    parser.add_argument("--dry-run", action="store_true", help="resolve but write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--seed", type=int, default=None, help="deterministic shuffle seed")
    args = parser.parse_args(argv)

    if not args.channels and not args.all:
        parser.error("name at least one channel, or pass --all")

    errors: list[str] = []
    channels: list[ResolvedChannel] = []
    try:
        workspace = load_workspace(_repo_root(args.repo_root))
        names = _select_channels(workspace, args)
        if not names:
            raise ConfigError("no channels found")
    except (ConfigError, MediaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # One broken channel must not stop the others from being compiled.
    for name in names:
        try:
            channels.append(load_channel(workspace, name))
        except (ConfigError, MediaError) as exc:
            errors.append(str(exc))

    results: list[dict] = []
    try:
        check_mount_uniqueness(channels)
        results = [compile_channel(workspace, c, args.dry_run, args.seed) for c in channels]
    except (ComposeError, ConfigError, MediaError) as exc:
        errors.append(str(exc))

    if args.json:
        print(
            json.dumps(
                {"warnings": workspace.warnings, "channels": results, "errors": errors}, indent=2
            )
        )
        return 1 if errors else 0

    for warning in workspace.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for result in results:
        verb = "would write" if result["dry_run"] else "wrote"
        print(
            f"{result['channel']}: {result['tracks']} track(s), {result['slides']} slide(s) "
            f"[{result['resolution']}@{result['fps']} {result['encoder']}]"
        )
        print(f"  {verb} {result['playlist']}")
        print(f"  {verb} {result['images_list']}")
        print(f"  {verb} {result['compose']}")
        for warning in result["warnings"]:
            print(f"  warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
