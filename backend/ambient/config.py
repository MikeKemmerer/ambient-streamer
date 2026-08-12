"""Load, merge and validate the four configuration surfaces.

Validation happens once, here, on load. Every rule in the validation table of
docs/contracts/config.md is a hard error: a channel that fails does not start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import ValidationError

from . import plugins as plugin_registry
from .ffmpeg_cmd import geometry
from .media import MediaError, MediaKind, MediaRoots, SelectionResult, resolve_selection
from .models import (
    CHANNEL_NAME_RE,
    AmbientConfig,
    ChannelConfig,
    ChannelEnv,
    ColorMode,
    Encoder,
    GlobalEnv,
    ImageOrder,
    Resolution,
)
from .presets import NEUTRAL_TARGETS, ColorTargets, color_targets


class ConfigError(ValueError):
    """Configuration is invalid; the channel must not start."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` reader — whole-line comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_yaml_mapping(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return raw


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}:"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  {location}: {err['msg']}")
    return "\n".join(lines)


@dataclass
class Workspace:
    """The repo root plus everything global, already validated."""

    root: Path
    common_dir: Path
    channels_dir: Path
    log_dir: Path
    run_dir: Path
    plugins_dir: Path
    presets_dir: Path
    ambient: AmbientConfig
    env: GlobalEnv
    warnings: list[str] = field(default_factory=list)


def _resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return Path(os.path.normpath(candidate))


def load_workspace(repo_root: Path | str) -> Workspace:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ConfigError(f"{root} is not a directory")

    warnings: list[str] = []

    ambient_path = root / "ambient.yaml"
    if ambient_path.is_file():
        try:
            ambient = AmbientConfig.model_validate(load_yaml_mapping(ambient_path))
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(ambient_path, exc)) from exc
    else:
        ambient = AmbientConfig()
        warnings.append(f"{ambient_path} is missing; using built-in defaults")

    env_path = root / ".env"
    raw_env = parse_env_file(env_path)
    if not env_path.is_file():
        warnings.append(f"{env_path} is missing; using built-in defaults")
    try:
        env = GlobalEnv.model_validate(raw_env)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(env_path, exc)) from exc

    # .env carries the install's host layout, so it wins where it is set;
    # ambient.yaml supplies the default when it is not.
    common_value = raw_env.get("AMBIENT_COMMON_DIR") or ambient.paths.common
    channels_value = raw_env.get("AMBIENT_DATA_DIR") or ambient.paths.channels
    log_value = raw_env.get("AMBIENT_LOG_DIR") or ambient.paths.logs
    # Shared with every composer so the watchdog can read their progress files.
    run_value = raw_env.get("AMBIENT_RUN_DIR") or "/run/ambient"

    workspace = Workspace(
        root=root,
        common_dir=_resolve_path(root, common_value),
        channels_dir=_resolve_path(root, channels_value),
        log_dir=_resolve_path(root, log_value),
        run_dir=_resolve_path(root, run_value),
        plugins_dir=root / "plugins",
        presets_dir=root / "presets",
        ambient=ambient,
        env=env,
        warnings=warnings,
    )
    if not workspace.common_dir.is_dir():
        workspace.warnings.append(f"{workspace.common_dir} does not exist")
    if not workspace.channels_dir.is_dir():
        raise ConfigError(f"{workspace.channels_dir} does not exist")
    return workspace


def channel_directory(workspace: Workspace, name: str) -> Path:
    """Validate a channel name and map it to its directory.

    The name arrives from a request in Phase 3 and reaches a Compose project
    name and a filesystem path, so it is checked against a strict pattern and
    the resulting path is re-checked against the channels root.
    """
    if not CHANNEL_NAME_RE.match(name or ""):
        raise ConfigError(
            f"invalid channel name {name!r}: must match {CHANNEL_NAME_RE.pattern}"
        )
    directory = Path(os.path.normpath(workspace.channels_dir / name))
    try:
        directory.relative_to(workspace.channels_dir)
    except ValueError as exc:
        raise ConfigError(f"channel {name!r} escapes {workspace.channels_dir}") from exc
    resolved = directory.resolve()
    try:
        resolved.relative_to(workspace.channels_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"channel {name!r} escapes {workspace.channels_dir} via a symlink") from exc
    return resolved


def ignored_channel_names(workspace: Workspace) -> list[str]:
    """Directories `paths.ignore_channels` holds back from discovery."""
    return sorted({name.strip() for name in workspace.ambient.paths.ignore_channels if name.strip()})


def discover_channels(workspace: Workspace) -> list[str]:
    """Every directory under channels/ that holds a .env and is not ignored."""
    ignored = set(ignored_channel_names(workspace))
    names: list[str] = []
    for child in sorted(workspace.channels_dir.iterdir()):
        if not child.is_dir() or not (child / ".env").is_file():
            continue
        if child.name in ignored:
            continue
        if not CHANNEL_NAME_RE.match(child.name):
            workspace.warnings.append(
                f"ignoring {child.name!r}: not a valid channel name"
            )
            continue
        names.append(child.name)
    return names


@dataclass
class ResolvedChannel:
    """One channel with all four surfaces merged and validated."""

    name: str
    directory: Path
    config: ChannelConfig
    env: ChannelEnv
    resolution: Resolution
    fps: int
    width: int
    height: int
    encoder: Encoder
    crossfade_seconds: float
    hold_seconds: float
    fade_seconds: float
    producer_fps: int
    jpeg_quality: int
    active_plugin: str
    hot_set: list[str]
    audio: SelectionResult
    images: SelectionResult
    bumpers: SelectionResult
    projected_cores: float = 0.0
    cores_breakdown: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def playlist_path(self) -> Path:
        return self.directory / "playlist.m3u"

    @property
    def images_list_path(self) -> Path:
        return self.directory / "images.list"

    @property
    def compose_path(self) -> Path:
        return self.directory / "docker-compose.yml"

    @property
    def shuffle_images(self) -> bool:
        return self.config.images.order is ImageOrder.SHUFFLE

    @property
    def color_mode(self) -> str:
        """What the compositor calls it; the config spells automatic in full."""
        return "manual" if self.config.color.mode is ColorMode.MANUAL else "auto"

    @property
    def color_initial(self) -> ColorTargets:
        """A filtergraph is fixed at launch, so a manual color starts baked in."""
        if self.config.color.mode is not ColorMode.MANUAL:
            return NEUTRAL_TARGETS
        return color_targets(self.config.color.manual.accent, self.config.color.manual.tint)

    @property
    def media_roots_common(self) -> Path:
        return self.directory


def load_channel(
    workspace: Workspace,
    name: str,
    *,
    resolve_media: bool = True,
) -> ResolvedChannel:
    directory = channel_directory(workspace, name)
    if not directory.is_dir():
        raise ConfigError(f"channel {name!r}: {directory} does not exist")

    config_path = directory / "config.yaml"
    if not config_path.is_file():
        raise ConfigError(f"channel {name!r}: {config_path} is missing")
    try:
        config = ChannelConfig.model_validate(load_yaml_mapping(config_path))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(config_path, exc)) from exc
    if config.name != name:
        raise ConfigError(
            f"{config_path}: name is {config.name!r} but the directory is {name!r}"
        )

    env_path = directory / ".env"
    if not env_path.is_file():
        raise ConfigError(f"channel {name!r}: {env_path} is missing")
    try:
        env = ChannelEnv.model_validate(parse_env_file(env_path))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(env_path, exc)) from exc

    defaults = workspace.ambient.defaults
    resolution = env.resolution or workspace.env.default_resolution or defaults.resolution
    fps = env.fps or workspace.env.default_fps or defaults.fps
    encoder = env.encoder or workspace.env.default_encoder or defaults.encoder
    width, height = geometry(resolution)

    warnings: list[str] = []
    if not env.stream_key.get_secret_value():
        warnings.append(f"channel {name!r}: YOUTUBE_STREAM_KEY is empty; it cannot publish")

    roots = MediaRoots.create(workspace.root, workspace.common_dir, directory)
    audio = SelectionResult()
    images = SelectionResult()
    bumpers = SelectionResult()
    if resolve_media:
        try:
            audio = resolve_selection(config.audio.tracks, MediaKind.AUDIO, roots)
            images = resolve_selection(config.images.slides, MediaKind.IMAGE, roots)
            if config.bumpers.enabled:
                bumpers = resolve_selection(config.bumpers.sources, MediaKind.AUDIO, roots)
        except MediaError as exc:
            raise ConfigError(f"channel {name!r}: {exc}") from exc
        warnings.extend(f"channel {name!r}: {w}" for w in audio.warnings + images.warnings)
        if not audio.files:
            raise ConfigError(
                f"channel {name!r}: audio.tracks resolves to no files; a channel with "
                "no audio cannot stream"
            )
        if not images.files:
            warnings.append(
                f"channel {name!r}: images.slides resolves to no files; the slideshow "
                "has nothing to show"
            )
        if config.bumpers.enabled and not bumpers.files:
            raise ConfigError(
                f"channel {name!r}: bumpers are enabled but bumpers.sources resolves to no files"
            )

    registry = plugin_registry.load_registry(workspace.plugins_dir)
    check = plugin_registry.check_hot_set(config.visualization.hot_set, registry, width, height, fps)
    if check.errors:
        raise ConfigError(f"channel {name!r}: " + "; ".join(check.errors))
    warnings.extend(f"channel {name!r}: {w}" for w in check.warnings)

    if config.preset:
        preset_path = workspace.presets_dir / f"{config.preset}.yaml"
        if not workspace.presets_dir.is_dir():
            warnings.append(
                f"channel {name!r}: presets/ does not exist; preset {config.preset!r} unverified"
            )
        elif not preset_path.is_file():
            raise ConfigError(f"channel {name!r}: preset {config.preset!r} does not exist")

    return ResolvedChannel(
        name=name,
        directory=directory,
        config=config,
        env=env,
        resolution=resolution,
        fps=fps,
        width=width,
        height=height,
        encoder=encoder,
        crossfade_seconds=(
            config.audio.crossfade_seconds
            if config.audio.crossfade_seconds is not None
            else defaults.crossfade_seconds
        ),
        hold_seconds=(
            config.images.hold_seconds
            if config.images.hold_seconds is not None
            else defaults.slideshow.hold_seconds
        ),
        fade_seconds=(
            config.images.fade_seconds
            if config.images.fade_seconds is not None
            else defaults.slideshow.fade_seconds
        ),
        producer_fps=defaults.slideshow.producer_fps,
        jpeg_quality=defaults.slideshow.jpeg_quality,
        active_plugin=config.visualization.active,
        hot_set=list(config.visualization.hot_set),
        audio=audio,
        images=images,
        bumpers=bumpers,
        projected_cores=check.projected_cores,
        cores_breakdown={
            "pipeline": round(check.pipeline_cores, 3),
            "preview": round(check.preview_cores, 3),
            "branches": round(check.branch_cores, 3),
        },
        warnings=warnings,
    )


def check_mount_uniqueness(channels: Iterable[ResolvedChannel]) -> None:
    """Two channels sharing an Icecast mount would fight over it."""
    seen: dict[str, str] = {}
    for channel in channels:
        for label, mount in (
            ("CHANNEL_MOUNT", channel.env.mount),
            ("CHANNEL_FALLBACK_MOUNT", channel.env.fallback_mount),
        ):
            owner = seen.get(mount)
            if owner is not None:
                raise ConfigError(
                    f"{label} {mount!r} is used by both {owner!r} and {channel.name!r}; "
                    "mounts must be unique across channels"
                )
            seen[mount] = channel.name


def check_capacity(workspace: Workspace, channels: Iterable[ResolvedChannel]) -> list[str]:
    """Refuse to oversubscribe the host into a stream that cannot hold 1.0x."""
    channels = list(channels)
    cores = float(os.cpu_count() or 1)
    reserved = workspace.ambient.limits.reserved_cores
    projected = sum(c.projected_cores for c in channels)
    if projected and projected > cores - reserved:
        raise ConfigError(
            f"projected cost {projected:.2f} cores for {len(channels)} channel(s) exceeds "
            f"{cores:.0f} cores minus {reserved:.2f} reserved"
        )
    if not projected:
        return ["plugin costs unknown; host capacity is unverified"]
    return []


def load_all_channels(
    workspace: Workspace, *, resolve_media: bool = True
) -> list[ResolvedChannel]:
    names = discover_channels(workspace)
    if len(names) > workspace.ambient.limits.max_channels:
        raise ConfigError(
            f"{len(names)} channels configured, limits.max_channels is "
            f"{workspace.ambient.limits.max_channels}"
        )
    channels = [load_channel(workspace, name, resolve_media=resolve_media) for name in names]
    check_mount_uniqueness(channels)
    workspace.warnings.extend(check_capacity(workspace, channels))
    return channels
