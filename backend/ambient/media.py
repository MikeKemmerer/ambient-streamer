"""Media selection resolver.

Implements the three selection forms, the expansion rules and the path
validation boundary from docs/contracts/media-selection.md, and writes the two
generated lists atomically.

Every path in a channel's config eventually arrives from the control plane's
HTTP API, so an entry is untrusted input: it is normalized, prefix-checked
against the two permitted trees, resolved through symlinks and prefix-checked
again. Globs are validated on their expanded results — a pattern is not a path
and cannot be prefix-checked.
"""

from __future__ import annotations

import glob as globlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from random import Random
from typing import Iterable, Sequence

AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})

CONTAINER_COMMON = "/media/common"
CONTAINER_CHANNEL = "/media/channel"

_MAGIC = re.compile(r"[*?\[]")


class MediaError(ValueError):
    """A selection entry is invalid or escapes the permitted trees."""


class MediaKind(str, Enum):
    AUDIO = "audio"
    IMAGE = "image"
    SOUNDBOARD = "soundboard"

    @property
    def extensions(self) -> frozenset[str]:
        return IMAGE_EXTENSIONS if self is MediaKind.IMAGE else AUDIO_EXTENSIONS

    @property
    def folder(self) -> str:
        return {
            MediaKind.AUDIO: "audio",
            MediaKind.IMAGE: "images",
            MediaKind.SOUNDBOARD: "soundboard",
        }[self]


@dataclass(frozen=True)
class MediaRoots:
    """The two trees a channel may draw from, plus the repo entries resolve against."""

    repo_root: Path
    common_dir: Path
    channel_dir: Path

    @staticmethod
    def create(repo_root: Path, common_dir: Path, channel_dir: Path) -> "MediaRoots":
        return MediaRoots(
            repo_root=Path(repo_root).resolve(),
            common_dir=Path(common_dir).resolve(),
            channel_dir=Path(channel_dir).resolve(),
        )

    @property
    def trees(self) -> tuple[Path, Path]:
        return (self.common_dir, self.channel_dir)


@dataclass(frozen=True)
class MediaFile:
    host_path: Path
    container_path: str
    origin: str


@dataclass
class SelectionResult:
    files: list[MediaFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    watched_dirs: list[Path] = field(default_factory=list)

    @property
    def container_paths(self) -> list[str]:
        return [f.container_path for f in self.files]


def discover_soundboard(roots: MediaRoots) -> SelectionResult:
    """Find shared and channel-local effects without following escaping links."""
    result = SelectionResult()
    for tree, origin in ((roots.common_dir, "common"), (roots.channel_dir, "channel")):
        folder = tree / MediaKind.SOUNDBOARD.folder
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*"), key=lambda item: natural_key(str(item))):
            if path.name.startswith(".") or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            real = path.resolve()
            if not real.is_file() or not _is_under(real, tree):
                continue
            result.files.append(
                MediaFile(
                    host_path=real,
                    container_path=to_container_path(real, roots),
                    origin=origin,
                )
            )
    return result


def natural_key(text: str) -> list[object]:
    """Sort key where `track2` precedes `track10`."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def is_glob(entry: str) -> bool:
    return bool(_MAGIC.search(entry))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _under_any(path: Path, roots: Iterable[Path]) -> bool:
    return any(_is_under(path, root) for root in roots)


def validate_entry(entry: str) -> str:
    """Reject entries that cannot be a safe repo-relative path."""
    if not entry or not entry.strip():
        raise MediaError("empty selection entry")
    if "\x00" in entry:
        raise MediaError("selection entry contains a NUL byte")
    if "\n" in entry or "\r" in entry:
        raise MediaError("selection entry contains a newline")
    if os.path.isabs(entry) or entry.startswith("~"):
        raise MediaError(
            f"{entry!r}: selection paths are relative to the repo root, "
            "absolute and home-relative paths are rejected"
        )
    return entry


def resolve_under_roots(entry: str, roots: MediaRoots) -> Path:
    """Normalize, prefix-check, resolve symlinks, prefix-check again."""
    lexical = Path(os.path.normpath(os.path.join(roots.repo_root, entry)))
    if not _under_any(lexical, roots.trees):
        raise MediaError(f"{entry!r} resolves outside common/ and this channel's directory")
    real = lexical.resolve()
    if not _under_any(real, roots.trees):
        raise MediaError(
            f"{entry!r} escapes common/ and this channel's directory once symlinks are resolved"
        )
    if "\n" in str(real) or "\r" in str(real):
        raise MediaError(f"{entry!r} resolves to a path containing a newline")
    return real


def to_container_path(host_path: Path, roots: MediaRoots) -> str:
    """Map a validated host path to the path the container will see."""
    if _is_under(host_path, roots.channel_dir):
        rel = host_path.relative_to(roots.channel_dir)
        return f"{CONTAINER_CHANNEL}/{rel.as_posix()}"
    if _is_under(host_path, roots.common_dir):
        rel = host_path.relative_to(roots.common_dir)
        return f"{CONTAINER_COMMON}/{rel.as_posix()}"
    raise MediaError(f"{host_path} is not inside a mounted tree")


def _accepted(path: Path, kind: MediaKind) -> bool:
    return (
        not path.name.startswith(".")
        and path.suffix.lower() in kind.extensions
        and path.is_file()
    )


def _glob_base(entry: str) -> str:
    """The leading path with no wildcard in it — the directory worth watching."""
    parts = entry.split("/")
    keep: list[str] = []
    for part in parts:
        if is_glob(part):
            break
        keep.append(part)
    return "/".join(keep[:-1]) if len(keep) == len(parts) else "/".join(keep)


def _expand_glob(entry: str, kind: MediaKind, roots: MediaRoots) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    pattern = entry[:-2] + "**/*" if entry.endswith("**") else entry
    matched = globlib.glob(pattern, root_dir=str(roots.repo_root), recursive=True)

    found: list[Path] = []
    escaped = 0
    for rel in matched:
        try:
            real = resolve_under_roots(rel, roots)
        except MediaError:
            escaped += 1
            continue
        if _accepted(real, kind):
            found.append(real)

    if escaped:
        warnings.append(
            f"{entry!r}: {escaped} match(es) discarded — they resolve outside "
            "common/ and this channel's directory"
        )
    if not found:
        warnings.append(f"{entry!r} matched no {kind.value} files")
    found.sort(key=lambda p: natural_key(str(p)))
    return found, warnings


def _expand_folder(kind: MediaKind, roots: MediaRoots) -> tuple[list[Path], list[str]]:
    """Form 1: the channel's own folder, recursively. Never common/."""
    warnings: list[str] = []
    base = roots.channel_dir / kind.folder
    if not base.is_dir():
        return [], [f"{base} does not exist; no {kind.value} selected"]

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                real = resolve_under_roots(str(candidate.relative_to(roots.repo_root)), roots)
            except (MediaError, ValueError):
                continue
            if _accepted(real, kind):
                found.append(real)
    if not found:
        warnings.append(f"{base} contains no {kind.value} files")
    found.sort(key=lambda p: natural_key(str(p)))
    return found, warnings


def resolve_selection(
    entries: Sequence[str],
    kind: MediaKind,
    roots: MediaRoots,
) -> SelectionResult:
    """Resolve one selection list into ordered, validated, in-container paths.

    Order is preserved as written and each glob expands in place. Duplicates
    keep their first occurrence.
    """
    result = SelectionResult()
    seen: set[Path] = set()

    def add(real: Path, origin: str) -> None:
        if real in seen:
            return
        seen.add(real)
        result.files.append(
            MediaFile(host_path=real, container_path=to_container_path(real, roots), origin=origin)
        )

    if not entries:
        found, warnings = _expand_folder(kind, roots)
        result.warnings.extend(warnings)
        folder = roots.channel_dir / kind.folder
        result.watched_dirs.append(folder)
        for real in found:
            add(real, f"<folder:{kind.folder}>")
        return result

    for entry in entries:
        validate_entry(entry)
        if is_glob(entry):
            found, warnings = _expand_glob(entry, kind, roots)
            result.warnings.extend(warnings)
            base = _glob_base(entry)
            if base:
                try:
                    watched = resolve_under_roots(base, roots)
                except MediaError as exc:
                    result.warnings.append(f"not watching {base!r}: {exc}")
                else:
                    if watched.is_dir() and watched not in result.watched_dirs:
                        result.watched_dirs.append(watched)
            for real in found:
                add(real, entry)
            continue

        real = resolve_under_roots(entry, roots)
        if not real.is_file():
            raise MediaError(f"{entry!r} does not exist")
        if real.suffix.lower() not in kind.extensions:
            raise MediaError(
                f"{entry!r} is not a recognized {kind.value} file "
                f"(expected one of {' '.join(sorted(kind.extensions))})"
            )
        add(real, entry)

    return result


# --------------------------------------------------------------------------
# Generated lists
# --------------------------------------------------------------------------


def atomic_write_lines(path: Path, lines: Iterable[str], mode: int = 0o644) -> None:
    """Write via a temp file in the SAME directory, then os.replace().

    A cross-device move degrades to copy-then-unlink and is not atomic; a
    partially written playlist is a stream outage.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(f"{line}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ordered_paths(
    result: SelectionResult, shuffle: bool = False, rng: Random | None = None
) -> list[str]:
    """Shuffling is applied when the file is written, so the UI order is the play order."""
    paths = result.container_paths
    if shuffle:
        paths = list(paths)
        (rng or Random()).shuffle(paths)
    return paths


def write_playlist(
    path: Path, result: SelectionResult, shuffle: bool = False, rng: Random | None = None
) -> list[str]:
    paths = ordered_paths(result, shuffle, rng)
    atomic_write_lines(path, paths)
    return paths


def write_images_list(
    path: Path, result: SelectionResult, shuffle: bool = False, rng: Random | None = None
) -> list[str]:
    """One absolute path per line and nothing else — timings would make the
    format whitespace-delimited, which breaks on filenames containing spaces."""
    paths = ordered_paths(result, shuffle, rng)
    atomic_write_lines(path, paths)
    return paths
