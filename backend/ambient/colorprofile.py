"""Color profile extraction.

One profile per image, beside the image tree it belongs to — a profile
describes the image, not the channel, so a shared image used by four channels
is analyzed once (docs/contracts/media-selection.md).

`extractor` is versioned so profiles can be regenerated when the algorithm
changes without guessing which are stale: a profile whose extractor is unknown
to the running backend is treated as absent and re-extracted.

Adding another implementation means registering it in `EXTRACTORS`; nothing
else here knows which one produced a profile.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from pydantic import Field, ValidationError

from .events import utc_now_iso
from .media import IMAGE_EXTENSIONS, atomic_write_lines
from .models import HEX_COLOR, StrictModel

LOG = logging.getLogger("ambient.colorprofile")

PROFILE_VERSION = 1
PILLOW_KMEANS_V1 = "pillow-kmeans-v1"

PALETTE_SIZE = 5
SAMPLE_EDGE = 160
KMEANS_ITERATIONS = 12
# Deterministic seeding: the same image must not produce a different accent
# every time the library is rescanned.
KMEANS_SEED = 20260811

LUMA = (0.2126, 0.7152, 0.0722)


class ProfileError(ValueError):
    """The image could not be read or analyzed."""


class ColorProfile(StrictModel):
    version: Literal[1] = PROFILE_VERSION
    source: str
    extracted_at: str
    extractor: str
    dominant: str = Field(pattern=HEX_COLOR)
    accent: str = Field(pattern=HEX_COLOR)
    palette: list[str] = Field(min_length=1)
    brightness: float = Field(ge=0.0, le=1.0)
    warmth: float = Field(ge=-1.0, le=1.0)
    mood: Literal["calm", "warm", "cool", "energetic"]


Rgb = tuple[float, float, float]
Extractor = Callable[[Path], "Analysis"]


@dataclass(frozen=True)
class Analysis:
    """What an extractor produces, before it becomes a stored profile."""

    dominant: str
    accent: str
    palette: list[str]
    brightness: float
    warmth: float
    mood: str


def to_hex(color: Sequence[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in color)


def _luma(color: Rgb) -> float:
    return sum(c * w for c, w in zip(color, LUMA)) / 255.0


def _saturation(color: Rgb) -> float:
    high, low = max(color), min(color)
    return 0.0 if high <= 0 else (high - low) / high


def _kmeans(pixels: Sequence[Rgb], k: int, iterations: int = KMEANS_ITERATIONS) -> list[tuple[Rgb, int]]:
    """Small k-means++ over sampled pixels. Seeded, so results are stable."""
    if not pixels:
        raise ProfileError("image has no pixels")
    rng = random.Random(KMEANS_SEED)
    k = max(1, min(k, len(set(pixels))))

    centers: list[Rgb] = [rng.choice(pixels)]
    while len(centers) < k:
        weights = [min(_distance(p, c) for c in centers) for p in pixels]
        total = sum(weights)
        if total <= 0:
            centers.append(rng.choice(pixels))
            continue
        centers.append(rng.choices(pixels, weights=weights, k=1)[0])

    assignments = [0] * len(pixels)
    for _ in range(iterations):
        moved = False
        for index, pixel in enumerate(pixels):
            best = min(range(len(centers)), key=lambda c: _distance(pixel, centers[c]))
            if best != assignments[index]:
                assignments[index] = best
                moved = True
        sums = [[0.0, 0.0, 0.0] for _ in centers]
        counts = [0] * len(centers)
        for index, pixel in enumerate(pixels):
            cluster = assignments[index]
            counts[cluster] += 1
            for channel in range(3):
                sums[cluster][channel] += pixel[channel]
        for cluster, count in enumerate(counts):
            if count:
                centers[cluster] = tuple(value / count for value in sums[cluster])  # type: ignore[assignment]
        if not moved:
            break

    counts = [0] * len(centers)
    for cluster in assignments:
        counts[cluster] += 1
    clusters = [(center, count) for center, count in zip(centers, counts) if count]
    clusters.sort(key=lambda item: item[1], reverse=True)
    return clusters


def _distance(a: Rgb, b: Rgb) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _mood(brightness: float, warmth: float, saturation: float) -> str:
    if saturation >= 0.55 and brightness >= 0.45:
        return "energetic"
    if warmth >= 0.15:
        return "warm"
    if warmth <= -0.15:
        return "cool"
    return "calm"


def extract_pillow_kmeans_v1(path: Path) -> Analysis:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ProfileError("Pillow is required to extract color profiles") from exc

    try:
        with Image.open(path) as handle:
            image = handle.convert("RGB")
            image.thumbnail((SAMPLE_EDGE, SAMPLE_EDGE))
            raw = image.tobytes()
    except (OSError, UnidentifiedImageError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    pixels: list[Rgb] = [
        (float(raw[i]), float(raw[i + 1]), float(raw[i + 2])) for i in range(0, len(raw), 3)
    ]

    clusters = _kmeans(pixels, PALETTE_SIZE)
    palette = [to_hex(center) for center, _count in clusters]
    dominant = clusters[0][0]

    # The accent is the most saturated cluster that is not the background.
    accent = max(
        (center for center, _ in clusters),
        key=lambda c: _saturation(c) * (0.35 + _luma(c)) + (0.0 if c == dominant else 0.15),
    )

    brightness = sum(_luma(p) for p in pixels) / len(pixels)
    warmth = sum((p[0] - p[2]) for p in pixels) / (len(pixels) * 255.0)
    warmth = max(-1.0, min(1.0, warmth * 2.0))
    saturation = sum(_saturation(p) for p in pixels) / len(pixels)

    return Analysis(
        dominant=to_hex(dominant),
        accent=to_hex(accent),
        palette=palette,
        brightness=round(brightness, 3),
        warmth=round(warmth, 3),
        mood=_mood(brightness, warmth, saturation),
    )


EXTRACTORS: dict[str, Extractor] = {PILLOW_KMEANS_V1: extract_pillow_kmeans_v1}
DEFAULT_EXTRACTOR = PILLOW_KMEANS_V1


def profile_path(image: Path, tree_root: Path) -> Path:
    """`<tree>/images/a/b.jpg` -> `<tree>/profiles/a/b.json`."""
    image = Path(image)
    try:
        relative = image.relative_to(Path(tree_root) / "images")
    except ValueError:
        relative = Path(image.name)
    return Path(tree_root) / "profiles" / relative.with_suffix(".json")


def read_profile(path: Path) -> ColorProfile | None:
    """A profile from an unknown extractor is treated as absent."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        profile = ColorProfile.model_validate(raw)
    except ValidationError:
        return None
    if profile.extractor not in EXTRACTORS:
        LOG.info("%s: extractor %r is unknown; re-extracting", path, profile.extractor)
        return None
    return profile


def extract(
    image: Path,
    *,
    repo_root: Path,
    extractor: str = DEFAULT_EXTRACTOR,
) -> ColorProfile:
    implementation = EXTRACTORS.get(extractor)
    if implementation is None:
        raise ProfileError(f"unknown extractor {extractor!r}")
    analysis = implementation(Path(image))
    try:
        source = str(Path(image).resolve().relative_to(Path(repo_root).resolve()).as_posix())
    except ValueError:
        source = Path(image).name
    return ColorProfile(
        source=source,
        extracted_at=utc_now_iso(),
        extractor=extractor,
        dominant=analysis.dominant,
        accent=analysis.accent,
        palette=analysis.palette,
        brightness=analysis.brightness,
        warmth=analysis.warmth,
        mood=analysis.mood,  # type: ignore[arg-type]
    )


def write_profile(profile: ColorProfile, path: Path) -> Path:
    body = json.dumps(profile.model_dump(), indent=2, sort_keys=False)
    atomic_write_lines(Path(path), body.splitlines())
    return Path(path)


def ensure_profile(
    image: Path,
    tree_root: Path,
    *,
    repo_root: Path,
    force: bool = False,
    extractor: str = DEFAULT_EXTRACTOR,
) -> tuple[ColorProfile, bool]:
    """Return the image's profile, extracting it if missing or stale."""
    target = profile_path(image, tree_root)
    if not force:
        existing = read_profile(target)
        if existing is not None and existing.extracted_at:
            try:
                fresh = Path(image).stat().st_mtime <= target.stat().st_mtime
            except OSError:
                fresh = True
            if fresh:
                return existing, False
    profile = extract(image, repo_root=repo_root, extractor=extractor)
    write_profile(profile, target)
    return profile, True


def iter_images(tree_root: Path) -> Iterable[Path]:
    images = Path(tree_root) / "images"
    if not images.is_dir():
        return []
    return sorted(
        path
        for path in images.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def refresh_tree(
    tree_root: Path,
    *,
    repo_root: Path,
    force: bool = False,
    extractor: str = DEFAULT_EXTRACTOR,
) -> dict[str, object]:
    """Extract every missing profile under one media tree."""
    extracted = 0
    skipped = 0
    errors: list[str] = []
    for image in iter_images(tree_root):
        try:
            _profile, wrote = ensure_profile(
                image, tree_root, repo_root=repo_root, force=force, extractor=extractor
            )
        except ProfileError as exc:
            errors.append(str(exc))
            continue
        extracted += int(wrote)
        skipped += int(not wrote)
    return {"tree": str(tree_root), "extracted": extracted, "skipped": skipped, "errors": errors}


def summarise(profile: ColorProfile | None) -> dict[str, object] | None:
    if profile is None:
        return None
    return {
        "dominant": profile.dominant,
        "accent": profile.accent,
        "brightness": profile.brightness,
        "warmth": profile.warmth,
        "mood": profile.mood,
        "extractor": profile.extractor,
    }


__all__ = [
    "Analysis",
    "ColorProfile",
    "EXTRACTORS",
    "PILLOW_KMEANS_V1",
    "ProfileError",
    "ensure_profile",
    "extract",
    "iter_images",
    "profile_path",
    "read_profile",
    "refresh_tree",
    "summarise",
    "write_profile",
]
