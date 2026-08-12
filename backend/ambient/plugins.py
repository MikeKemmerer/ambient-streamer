"""Visualisation plugin registry.

Reads the manifests under `plugins/` and enforces the one rule FFmpeg will not
enforce: a plugin branch must emit exactly the channel's output geometry. A
size mismatch produces corrupted output with exit code 0 and no error message
(docs/contracts/plugin.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_LITERAL_SIZE = re.compile(r"\b(?:s|size)=(\d+)x(\d+)")
_PLACEHOLDER_SIZE = re.compile(r"\$\{WIDTH\}x\$\{HEIGHT\}")

# 720p30 is the measurement baseline; anything larger scales from it.
_BASE_PIXELS = 1280 * 720
_1080P_PIXELS = 1920 * 1080


class PluginError(ValueError):
    """A plugin manifest is missing, malformed, or geometrically wrong."""


@dataclass(frozen=True)
class Commandable:
    target: str
    param: str
    type: str


@dataclass(frozen=True)
class PluginManifest:
    name: str
    directory: Path
    display_name: str = ""
    description: str = ""
    version: str = "0.0.0"
    author: str = ""
    commandable: tuple[Commandable, ...] = ()
    cores_720p30: float = 0.25
    scale_1080p: float = 1.9
    requires_filters: tuple[str, ...] = ()
    declared_size: str | None = None

    @property
    def fragment_path(self) -> Path:
        return self.directory / "viz.ffmpeg"

    def cost_cores(self, width: int, height: int, fps: int) -> float:
        pixels = width * height
        if pixels <= _BASE_PIXELS:
            scale = 1.0
        elif pixels <= _1080P_PIXELS:
            scale = 1.0 + (self.scale_1080p - 1.0) * (pixels - _BASE_PIXELS) / (
                _1080P_PIXELS - _BASE_PIXELS
            )
        else:
            scale = self.scale_1080p * pixels / _1080P_PIXELS
        return self.cores_720p30 * scale * (fps / 30.0)


def load_manifest(directory: Path) -> PluginManifest:
    manifest_path = directory / "config.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginError(f"{manifest_path} is missing") from exc
    except json.JSONDecodeError as exc:
        raise PluginError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw.get("name"):
        raise PluginError(f"{manifest_path} has no 'name'")

    cost = raw.get("cost") or {}
    commandable = tuple(
        Commandable(
            target=str(item.get("target", "")),
            param=str(item.get("param", "")),
            type=str(item.get("type", "")),
        )
        for item in raw.get("commandable", [])
        if isinstance(item, dict)
    )
    return PluginManifest(
        name=str(raw["name"]),
        directory=directory,
        display_name=str(raw.get("display_name", raw["name"])),
        description=str(raw.get("description", "")),
        version=str(raw.get("version", "0.0.0")),
        author=str(raw.get("author", "")),
        commandable=commandable,
        cores_720p30=float(cost.get("cores_720p30", 0.25)),
        scale_1080p=float(cost.get("scale_1080p", 1.9)),
        requires_filters=tuple(str(f) for f in raw.get("requires_filters", [])),
        declared_size=raw.get("output_size"),
    )


def load_registry(plugins_dir: Path) -> dict[str, PluginManifest]:
    plugins_dir = Path(plugins_dir)
    registry: dict[str, PluginManifest] = {}
    if not plugins_dir.is_dir():
        return registry
    for child in sorted(plugins_dir.iterdir()):
        if child.is_dir() and (child / "config.json").is_file():
            manifest = load_manifest(child)
            registry[manifest.name] = manifest
    return registry


def check_output_size(manifest: PluginManifest, width: int, height: int) -> None:
    """Hard-fail a branch that would not emit the channel's exact geometry."""
    declared = manifest.declared_size
    if declared and declared != "channel":
        match = re.fullmatch(r"(\d+)x(\d+)", str(declared))
        if not match:
            raise PluginError(f"plugin {manifest.name!r}: output_size {declared!r} is unparseable")
        if (int(match.group(1)), int(match.group(2))) != (width, height):
            raise PluginError(
                f"plugin {manifest.name!r} declares {declared}, channel is {width}x{height}; "
                "a size mismatch corrupts output silently"
            )
        return

    fragment = manifest.fragment_path
    if not fragment.is_file():
        raise PluginError(f"plugin {manifest.name!r}: {fragment} is missing")
    text = fragment.read_text(encoding="utf-8")
    if _PLACEHOLDER_SIZE.search(text):
        return
    literals = {(int(w), int(h)) for w, h in _LITERAL_SIZE.findall(text)}
    if not literals:
        raise PluginError(
            f"plugin {manifest.name!r} declares no output size; it must use "
            "${WIDTH}x${HEIGHT} or a literal matching the channel"
        )
    bad = sorted(size for size in literals if size != (width, height))
    if bad:
        pretty = ", ".join(f"{w}x{h}" for w, h in bad)
        raise PluginError(
            f"plugin {manifest.name!r} emits {pretty}, channel is {width}x{height}; "
            "a size mismatch corrupts output silently"
        )


@dataclass
class HotSetCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    projected_cores: float = 0.0


def check_hot_set(
    hot_set: list[str],
    registry: dict[str, PluginManifest],
    width: int,
    height: int,
    fps: int,
) -> HotSetCheck:
    check = HotSetCheck()
    if not registry:
        check.warnings.append(
            "no plugin manifests found; hot_set membership and output size are unverified"
        )
        return check
    for name in hot_set:
        manifest = registry.get(name)
        if manifest is None:
            check.errors.append(f"plugin {name!r} in hot_set does not exist in the registry")
            continue
        try:
            check_output_size(manifest, width, height)
        except PluginError as exc:
            check.errors.append(str(exc))
        check.projected_cores += manifest.cost_cores(width, height, fps)
    return check
