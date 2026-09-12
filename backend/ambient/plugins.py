"""Visualization plugin registry.

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

# Measured at 720p30/libx264 on a 7-core host. A filter benchmark sees only the
# visualization branch; a channel also pays MP3 decode, the slideshow, the
# program encode and the preview, so budget from these, never from a manifest.
#
#   3 hot plugins  0.9936x at 2.03 cores
#   5 hot plugins  0.97x   at 2.64 cores  (below realtime)
#   1 hot plugin   1.0x    at ~1.50 cores
#
# Slope is 0.28 per extra hot branch, not the 0.22 previously assumed.
IDLE_BRANCH_CORES_720P30 = 0.28
# Decode, slideshow, per-frame filters and the 720p program encode.
PIPELINE_CORES_720P30 = 1.02
# The HLS preview is a second complete encode, not a tap off the first. Fixed
# at 640x360@15 regardless of the channel's own geometry, so it does not scale.
PREVIEW_CORES = 0.20
# The pipeline scales with pixels like the visualization branches do.
PIPELINE_SCALE_1080P = 1.9


class PluginError(ValueError):
    """A plugin manifest is missing, malformed, or geometrically wrong."""


def pixel_scale(width: int, height: int, fps: int, scale_1080p: float) -> float:
    """Cost multiplier for a branch measured at 720p30."""
    pixels = width * height
    if pixels <= _BASE_PIXELS:
        scale = 1.0
    elif pixels <= _1080P_PIXELS:
        scale = 1.0 + (scale_1080p - 1.0) * (pixels - _BASE_PIXELS) / (
            _1080P_PIXELS - _BASE_PIXELS
        )
    else:
        scale = scale_1080p * pixels / _1080P_PIXELS
    return scale * (fps / 30.0)


@dataclass(frozen=True)
class Commandable:
    target: str
    param: str
    type: str


@dataclass(frozen=True)
class Parameter:
    """One knob a plugin exposes, substituted into its fragment as ${token}."""

    name: str
    token: str
    label: str = ""
    description: str = ""
    type: str = "float"  # float | int | bool | enum
    minimum: float = 0.0
    maximum: float = 1.0
    step: float = 0.1
    default: float | int | bool | str = 0
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)

    def coerce(self, value: object) -> float | int | bool | str:
        """Clamp rather than reject: a fragment must always be substitutable.

        An out-of-range number in a filter argument does not fail loudly - FFmpeg
        takes the option, ignores it, and the branch renders wrong with exit 0.
        """
        if self.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if self.type == "enum":
            allowed = [choice for choice, _label in self.choices]
            text = str(value)
            if text not in allowed:
                raise PluginError(
                    f"{self.name}: {text!r} is not one of {', '.join(allowed)}"
                )
            return text
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise PluginError(f"{self.name}: {value!r} is not a number") from exc
        number = max(self.minimum, min(self.maximum, number))
        return int(round(number)) if self.type == "int" else number


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
    parameters: tuple[Parameter, ...] = ()

    @property
    def fragment_path(self) -> Path:
        return self.directory / "viz.ffmpeg"

    def resolve_parameters(self, chosen: dict[str, object] | None) -> dict[str, object]:
        """Every declared token gets a value, so no ${token} can survive into the graph."""
        chosen = chosen or {}
        resolved: dict[str, object] = {}
        for parameter in self.parameters:
            if parameter.name in chosen:
                resolved[parameter.name] = parameter.coerce(chosen[parameter.name])
            else:
                resolved[parameter.name] = parameter.default
        return resolved

    def cost_cores(self, width: int, height: int, fps: int) -> float:
        """Declared cost, floored at the measured per-branch slope.

        Manifest figures come from benchmarking a branch alone; in a live graph
        the cheapest branch still measured 0.28 at 720p30.
        """
        scale = pixel_scale(width, height, fps, self.scale_1080p)
        floor = IDLE_BRANCH_CORES_720P30 * pixel_scale(width, height, fps, PIPELINE_SCALE_1080P)
        return max(self.cores_720p30 * scale, floor)


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
        parameters=_load_parameters(manifest_path, raw.get("parameters", [])),
    )


def _load_parameters(manifest_path: Path, raw: object) -> tuple[Parameter, ...]:
    if not isinstance(raw, list):
        raise PluginError(f"{manifest_path}: 'parameters' must be a list")
    parameters: list[Parameter] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            raise PluginError(f"{manifest_path}: every parameter needs a 'name'")
        kind = str(item.get("type", "float"))
        if kind not in {"float", "int", "bool", "enum"}:
            raise PluginError(f"{manifest_path}: unknown parameter type {kind!r}")
        choices = tuple(
            (str(c.get("value")), str(c.get("label", c.get("value"))))
            for c in item.get("choices", [])
            if isinstance(c, dict)
        )
        if kind == "enum" and not choices:
            raise PluginError(f"{manifest_path}: enum parameter {item['name']!r} has no choices")
        parameters.append(Parameter(
            name=str(item["name"]),
            token=str(item.get("token", str(item["name"]).upper())),
            label=str(item.get("label", item["name"])),
            description=str(item.get("description", "")),
            type=kind,
            minimum=float(item.get("min", 0)),
            maximum=float(item.get("max", 1)),
            step=float(item.get("step", 1 if kind == "int" else 0.1)),
            default=item.get("default", choices[0][0] if choices else 0),
            choices=choices,
        ))
    return tuple(parameters)


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
    pipeline_cores: float = 0.0
    preview_cores: float = 0.0
    branch_cores: float = 0.0


def pipeline_cores(width: int, height: int, fps: int) -> float:
    """Everything the compositor pays before any visualization branch."""
    return PIPELINE_CORES_720P30 * pixel_scale(width, height, fps, PIPELINE_SCALE_1080P)


def check_visualization(
    active: str,
    registry: dict[str, PluginManifest],
    width: int,
    height: int,
    fps: int,
    *,
    enabled: bool = True,
) -> HotSetCheck:
    check = HotSetCheck()
    layer_width = min(width, 1280)
    layer_height = min(height, 720)
    layer_fps = min(fps, 30)
    check.pipeline_cores = pipeline_cores(width, height, fps)
    check.preview_cores = PREVIEW_CORES
    if not enabled:
        check.projected_cores = check.pipeline_cores + check.preview_cores
        return check
    if not registry:
        check.warnings.append(
            "no plugin manifests found; active plugin and output size are unverified"
        )
        check.projected_cores = check.pipeline_cores + check.preview_cores
        return check
    manifest = registry.get(active)
    if manifest is None:
        check.errors.append(f"active plugin {active!r} does not exist in the registry")
        return check
    try:
        check_output_size(manifest, layer_width, layer_height)
    except PluginError as exc:
        check.errors.append(str(exc))
    check.branch_cores = manifest.cost_cores(layer_width, layer_height, layer_fps)
    check.projected_cores = check.pipeline_cores + check.preview_cores + check.branch_cores
    return check
