"""Operator media upload — a security boundary, not a convenience.

This process holds the Docker socket, which is root-equivalent on the host, so
every byte of an upload is hostile until proven otherwise:

* the submitted filename is **normalized first, then validated** — unicode
  folded, separators unified, percent-escapes decoded for detection, reduced to
  a basename. Checking before normalizing is the classic way to get this wrong.
* the destination is confined to `<tree>/audio`, `<tree>/images` or
    `<tree>/soundboard` for exactly
  one of the two trees in docs/contracts/media-selection.md, re-checked after
  symlink resolution.
* the extension allowlist is the contract's, and the **content is probed** —
  ffprobe for audio, Pillow for images. Extension and Content-Type are both
  chosen by the caller and prove nothing.
* size and free space are enforced **while the body streams**, straight into a
  temp file in the destination directory; nothing is buffered whole.
* publication is `os.link()` then unlink, which is atomic and fails rather than
  clobbering media a running channel may be reading. A cross-device `mv` from
  /tmp is neither.

The browser UI appends the file part *before* the fields that name the
destination, so the target cannot always be known when the bytes start
arriving. Such a request is **deferred**: the body streams into a staging
directory beside the two trees, and the target is resolved once the whole body
has been read. The publish is still `os.link()` inside the destination
directory, so a watcher still never sees a partial file.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, AsyncIterator, Awaitable, Callable
from urllib.parse import unquote

from .media import MediaKind
from .models import AmbientConfig

LOG = logging.getLogger("ambient.uploads")

COMMON_TARGET = "common"
CHUNK_BYTES = 256 * 1024
# ext4 allows 255; the temp file adds a "." prefix and a ".part" suffix.
MAX_FILENAME_BYTES = 200
SNIFF_BYTES = 32
MAX_DEDUP_ATTEMPTS = 999
FFPROBE_TIMEOUT = 20.0
MAX_FIELD_BYTES = 4096
MAX_FIELDS = 16
# Deferred uploads stage here: beside the two trees, so the move into the
# destination directory is a rename rather than a copy.
STAGING_DIRNAME = ".uploads"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
# `:` `*` `?` `"` `<` `>` `|` are rejected outright; separators are handled above.
_ILLEGAL_CHARS = frozenset(':*?"<>|')

KIND_BY_SEGMENT = {
    "audio": MediaKind.AUDIO,
    "images": MediaKind.IMAGE,
    "soundboard": MediaKind.SOUNDBOARD,
}
# The UI names the plural folder, the enum names the singular kind; take either.
KIND_ALIASES = {
    "audio": MediaKind.AUDIO,
    "image": MediaKind.IMAGE,
    "images": MediaKind.IMAGE,
    "soundboard": MediaKind.SOUNDBOARD,
}

# Pillow's format name -> the extensions that may legitimately carry it.
IMAGE_FORMATS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
    "BMP": frozenset({".bmp"}),
    "DIB": frozenset({".bmp"}),
}

_ffprobe_missing_logged = False


class UploadError(ValueError):
    """One file was rejected. `error` is a stable machine token."""

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


class UploadAborted(Exception):
    """The whole request is refused; no further parts are read."""

    def __init__(self, error: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.status_code = status_code


# --------------------------------------------------------------------------
# 1. Filename sanitization
# --------------------------------------------------------------------------


def sanitize_filename(raw: str | None) -> str:
    """Reduce a submitted filename to a safe basename, or reject it.

    Normalization happens first so validation sees what the filesystem would.
    """
    if raw is None or not raw.strip():
        raise UploadError("invalid_filename", "the upload part carries no filename")

    normalized = unicodedata.normalize("NFC", raw)
    if _CONTROL.search(normalized):
        raise UploadError("invalid_filename", "the filename contains control characters")

    unified = normalized.replace("\\", "/")
    # Percent-escapes are decoded only to detect an encoded separator; a
    # filename that hides `../` behind `%2e%2e%2f` is an attempt, not a name.
    decoded = unquote(unified)
    if _CONTROL.search(decoded):
        raise UploadError("invalid_filename", "the filename contains encoded control characters")

    for probe in (unified, decoded):
        if probe.startswith("/") or _DRIVE_LETTER.match(probe):
            raise UploadError("invalid_filename", f"{_echo(raw)}: absolute paths are rejected")
        if any(part == ".." for part in probe.split("/")):
            raise UploadError("invalid_filename", f"{_echo(raw)}: '..' is rejected")

    base = posixpath.basename(unified).strip()
    if not base or base in (".", ".."):
        raise UploadError("invalid_filename", f"{_echo(raw)}: no usable filename")
    if base.startswith("."):
        raise UploadError("invalid_filename", f"{_echo(raw)}: leading dots are rejected")
    if any(ch in _ILLEGAL_CHARS for ch in base):
        raise UploadError("invalid_filename", f"{_echo(base)}: contains a forbidden character")
    if len(base.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise UploadError(
            "invalid_filename", f"the filename is longer than {MAX_FILENAME_BYTES} bytes"
        )
    return base


def _echo(raw: str) -> str:
    """A caller-supplied string, made safe to put in a response or a log."""
    return repr(_CONTROL.sub("", raw)[:120])


def check_extension(filename: str, kind: MediaKind) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in kind.extensions:
        raise UploadError(
            "unsupported_extension",
            f"{_echo(filename)}: not a recognized {kind.value} file "
            f"(expected one of {' '.join(sorted(kind.extensions))})",
        )
    return suffix


# --------------------------------------------------------------------------
# 2. Destination confinement
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadTarget:
    """Where one request writes: `common` or one channel, for one media kind."""

    name: str
    kind: MediaKind
    tree_root: Path
    channel: str | None

    @property
    def directory(self) -> Path:
        return self.tree_root / self.kind.folder


# Resolves a deferred target from the form fields collected off the body.
TargetResolver = Callable[[dict[str, str]], UploadTarget]


def resolve_destination(target: UploadTarget, filename: str) -> Path:
    """The only writable shape is `<tree>/<audio|images>/<basename>`.

    Every component is resolved before it is checked, so a symlinked media
    folder — or a symlink already sitting at the destination name — cannot
    redirect the write outside the tree.
    """
    root = Path(target.tree_root).resolve()
    folder = target.kind.folder
    directory = Path(os.path.realpath(root / folder))
    if directory.parent != root or directory.name != folder:
        raise UploadError("forbidden_target", f"{folder}/ does not resolve inside {root}")

    candidate = Path(os.path.normpath(directory / filename))
    if candidate.parent != directory or candidate.name != filename:
        raise UploadError("forbidden_target", f"{_echo(filename)} escapes {directory}")
    resolved = Path(os.path.realpath(candidate))
    if resolved.parent != directory:
        raise UploadError(
            "forbidden_target", f"{_echo(filename)} escapes {directory} once symlinks are resolved"
        )
    return candidate


# --------------------------------------------------------------------------
# 3. Limits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadLimits:
    max_file_bytes: int
    max_request_bytes: int
    max_files: int
    min_free_bytes: int

    @staticmethod
    def from_config(ambient: AmbientConfig) -> "UploadLimits":
        settings = ambient.uploads
        mb = 1024 * 1024
        return UploadLimits(
            max_file_bytes=settings.max_file_mb * mb,
            max_request_bytes=settings.max_request_mb * mb,
            max_files=settings.max_files,
            min_free_bytes=settings.min_free_mb * mb,
        )


def free_bytes(directory: Path) -> int:
    try:
        return shutil.disk_usage(str(directory)).free
    except OSError:
        return 0


# --------------------------------------------------------------------------
# 4. Content probing — the bytes, never the extension
# --------------------------------------------------------------------------


def sniff_audio(head: bytes, suffix: str) -> bool:
    """Container magic for the seven allowed audio extensions."""
    if len(head) < 12:
        return False
    if head[:3] == b"ID3":  # a tag may precede any of them
        return True
    if suffix == ".flac":
        return head[:4] == b"fLaC"
    if suffix in (".ogg", ".opus"):
        return head[:4] == b"OggS"
    if suffix == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if suffix in (".m4a", ".aac"):
        if head[4:8] == b"ftyp":
            return True
        return suffix == ".aac" and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0
    if suffix == ".mp3":
        return head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    return False


def ffprobe_streams(path: Path, timeout: float = FFPROBE_TIMEOUT) -> list[str] | None:
    """Codec types ffprobe finds, or None when ffprobe is unavailable."""
    global _ffprobe_missing_logged
    binary = shutil.which("ffprobe")
    if binary is None:
        if not _ffprobe_missing_logged:
            LOG.warning(
                "ffprobe is not on PATH; uploaded audio is accepted on its container "
                "magic alone. Install ffmpeg in this image to restore the full probe."
            )
            _ffprobe_missing_logged = True
        return None
    try:
        completed = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=nw=1:nk=1",
                f"file:{path}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def validate_audio(path: Path, head: bytes, suffix: str) -> str:
    if not sniff_audio(head, suffix):
        raise UploadError("unsupported_content", "the bytes are not a recognized audio container")
    streams = ffprobe_streams(path)
    if streams is None:
        return "audio (container magic; ffprobe unavailable)"
    if "audio" not in streams:
        raise UploadError("unsupported_content", "ffprobe found no audio stream")
    return "audio"


def validate_image(path: Path, suffix: str) -> str:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise UploadError("unsupported_content", f"Pillow is required to verify images: {exc}")

    try:
        with Image.open(path) as handle:
            handle.verify()
            image_format = (handle.format or "").upper()
    except Image.DecompressionBombError:
        raise UploadError("unsupported_content", "the image is implausibly large") from None
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        raise UploadError("unsupported_content", "the bytes do not decode as an image") from None

    allowed = IMAGE_FORMATS.get(image_format)
    if allowed is None:
        raise UploadError("unsupported_content", f"{image_format or 'unknown'} images are not accepted")
    if suffix not in allowed:
        raise UploadError(
            "unsupported_content",
            f"the content is {image_format} but the name says {suffix}",
        )
    return image_format


def validate_content(path: Path, head: bytes, suffix: str, kind: MediaKind) -> str:
    if kind is MediaKind.IMAGE:
        return validate_image(path, suffix)
    return validate_audio(path, head, suffix)


# --------------------------------------------------------------------------
# 5. Atomic, non-clobbering publish
# --------------------------------------------------------------------------


def dedup_path(target: Path) -> Path:
    """`x.mp3` -> `x-2.mp3` -> `x-3.mp3`, never an existing name."""
    if not os.path.lexists(target):
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, MAX_DEDUP_ATTEMPTS + 1):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise UploadError("already_exists", f"{target.name}: too many files with this name")


def publish(tmp_path: Path, target: Path, *, rename_on_conflict: bool) -> tuple[Path, bool]:
    """Link the temp file into place. Never overwrites, never partially visible."""
    final = dedup_path(target) if rename_on_conflict else target
    try:
        os.link(tmp_path, final)
    except FileExistsError:
        raise UploadError("already_exists", f"{final.name} already exists") from None
    except OSError:
        # A filesystem without hard links: replace() is still atomic, so check
        # first and accept the (tiny) race rather than leaving a partial file.
        if os.path.lexists(final):
            raise UploadError("already_exists", f"{final.name} already exists") from None
        os.replace(tmp_path, final)
        return final, final != target
    os.unlink(tmp_path)
    return final, final != target


# --------------------------------------------------------------------------
# 6. Streaming receiver
# --------------------------------------------------------------------------


@dataclass
class UploadResult:
    submitted: str
    ok: bool = False
    filename: str | None = None
    path: str | None = None
    bytes: int = 0
    renamed: bool = False
    error: str | None = None
    detail: str | None = None
    profile: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            # `name` echoes what the client sent, so it can match its own row
            # even when the file was stored under a de-duplicated `filename`.
            "name": self.submitted,
            "submitted": self.submitted,
            "ok": self.ok,
            "filename": self.filename,
            "path": self.path,
            "bytes": self.bytes,
            "renamed": self.renamed,
            "error": self.error,
            "detail": self.detail,
            "profile": self.profile,
        }


@dataclass
class _Part:
    submitted: str = ""
    is_file: bool = False
    field_name: str = ""
    value: bytes = b""
    filename: str | None = None
    suffix: str = ""
    tmp_path: Path | None = None
    handle: IO[bytes] | None = None
    written: int = 0
    head: bytes = b""
    budget: int = 0
    over_limit: str | None = None
    failure: UploadError | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)


class UploadReceiver:
    """Drives python-multipart, writing each part into the destination dir.

    The parser's callbacks are synchronous, so they only sanitize and write.
    Probing, publishing and profile extraction are CPU-bound, so a finished
    part is queued and finalized off the event loop by the caller.

    With no `target`, the destination comes from form fields that may arrive
    after the file: parts stage in `staging_dir` and `resolve_target` is called
    with the collected fields once the body has been read.
    """

    def __init__(
        self,
        target: UploadTarget | None,
        limits: UploadLimits,
        *,
        rename_on_conflict: bool = False,
        repo_root: Path | None = None,
        resolve_target: TargetResolver | None = None,
        staging_dir: Path | None = None,
    ) -> None:
        if target is None and (resolve_target is None or staging_dir is None):
            raise ValueError("a deferred target needs a resolver and a staging directory")
        self.target = target
        self.limits = limits
        self.rename_on_conflict = rename_on_conflict
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.resolve_target = resolve_target
        self.staging_dir = Path(staging_dir) if staging_dir is not None else None
        self.deferred = target is None
        self.fields: dict[str, str] = {}
        self.results: list[UploadResult] = []
        self.pending: list[_Part] = []
        self.files = 0
        self.total = 0
        self._part = _Part()
        self._header_field = b""
        self._header_value = b""

    @property
    def finalize_early(self) -> bool:
        """A deferred part cannot be finalized until the trailing fields land."""
        return not self.deferred

    def resolve(self) -> UploadTarget:
        if self.target is None:
            assert self.resolve_target is not None
            self.target = self.resolve_target(self.fields)
        return self.target

    # ------------------------------------------------------------ callbacks

    def on_part_begin(self) -> None:
        self._part = _Part()
        self._header_field = b""
        self._header_value = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._part.headers.append((self._header_field.lower(), self._header_value))
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        from python_multipart.multipart import parse_options_header

        disposition = b""
        for name, value in self._part.headers:
            if name == b"content-disposition":
                disposition = value
        _kind, options = parse_options_header(disposition)
        raw = options.get(b"filename")
        if raw is None:
            self._part.is_file = False
            self._part.field_name = _decode(options.get(b"name", b""))[:64]
            if len(self.fields) >= MAX_FIELDS:
                raise UploadAborted(
                    "too_many_fields", f"at most {MAX_FIELDS} form fields per request", 400
                )
            return

        self._part.is_file = True
        self.files += 1
        if self.files > self.limits.max_files:
            raise UploadAborted(
                "too_many_files", f"at most {self.limits.max_files} files per request", 400
            )
        submitted = _decode(raw)
        self._part.submitted = _CONTROL.sub("", submitted)[:120]
        try:
            filename = sanitize_filename(submitted)
            self._part.filename = filename
            # Deferred: the kind is a trailing field, so the allowlist is
            # applied in `_settle` instead. The size limit still caps the bytes.
            if self.target is not None:
                self._part.suffix = check_extension(filename, self.target.kind)
            self._open(filename)
        except UploadError as exc:
            self._part.failure = exc

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        self.total += len(chunk)
        if self.total > self.limits.max_request_bytes:
            raise UploadAborted(
                "request_too_large",
                f"the request exceeds {self.limits.max_request_bytes} bytes",
                413,
            )
        part = self._part
        if not part.is_file:
            part.value += chunk[: max(0, MAX_FIELD_BYTES - len(part.value))]
            return
        if part.handle is None:
            return
        if part.written + len(chunk) > part.budget:
            part.over_limit = part.over_limit or _limit_error(self.limits, part.budget)
            self._discard(part)
            return
        try:
            part.handle.write(chunk)
        except OSError as exc:
            part.failure = UploadError("write_failed", f"could not write the upload: {exc}")
            self._discard(part)
            return
        part.written += len(chunk)
        if len(part.head) < SNIFF_BYTES:
            part.head = (part.head + chunk)[:SNIFF_BYTES]

    def on_part_end(self) -> None:
        part = self._part
        if part.is_file:
            self.pending.append(part)
        elif part.field_name:
            if part.field_name in self.fields:
                raise UploadAborted(
                    "duplicate_field", f"{part.field_name!r} appears more than once", 400
                )
            self.fields[part.field_name] = _CONTROL.sub("", _decode(part.value)).strip()
        self._part = _Part()

    def on_end(self) -> None:
        return None

    # -------------------------------------------------------------- helpers

    def _open(self, filename: str) -> None:
        directory = self.target.directory if self.target is not None else self._staging()
        directory.mkdir(parents=True, exist_ok=True)
        if self.target is not None:
            destination = resolve_destination(self.target, filename)
            if not self.rename_on_conflict and os.path.lexists(destination):
                raise UploadError("already_exists", f"{filename} already exists")

        self._part.budget = self._require_space(directory)
        fd, tmp = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=str(directory))
        self._part.tmp_path = Path(tmp)
        self._part.handle = os.fdopen(fd, "wb")

    def _staging(self) -> Path:
        assert self.staging_dir is not None
        return self.staging_dir

    def _require_space(self, directory: Path, needed: int = 0) -> int:
        available = free_bytes(directory) - self.limits.min_free_bytes
        if available <= needed:
            raise UploadError(
                "insufficient_space",
                f"less than {self.limits.min_free_bytes} bytes would remain free",
            )
        return min(self.limits.max_file_bytes, available)

    def _settle(self, part: _Part) -> UploadTarget:
        """Resolve a deferred target and move the staged file into it.

        The checks `_open` makes up front for a known target — allowlist, free
        space, no collision — happen here instead, then the file is renamed into
        the destination directory so the publish is a same-directory link.
        """
        target = self.resolve()
        if not self.deferred or part.tmp_path is None:
            return target

        filename = str(part.filename)
        part.suffix = check_extension(filename, target.kind)
        directory = target.directory
        directory.mkdir(parents=True, exist_ok=True)
        destination = resolve_destination(target, filename)
        if not self.rename_on_conflict and os.path.lexists(destination):
            raise UploadError("already_exists", f"{filename} already exists")
        self._require_space(directory, part.written)
        part.tmp_path = self._relocate(part.tmp_path, directory, filename)
        return target

    def _relocate(self, staged: Path, directory: Path, filename: str) -> Path:
        """A rename when staging shares the tree's filesystem, a copy when the
        trees are separate mounts. Either way the file is whole before it is
        published, and it wears a dot-prefixed `.part` name until then."""
        fd, tmp = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=str(directory))
        os.close(fd)
        try:
            os.replace(staged, tmp)
        except OSError:
            try:
                with open(staged, "rb") as source, open(tmp, "wb") as sink:
                    shutil.copyfileobj(source, sink, CHUNK_BYTES)
                    sink.flush()
                    os.fsync(sink.fileno())
            except OSError:
                _unlink(tmp)
                raise
            _unlink(staged)
        return Path(tmp)

    def _discard(self, part: _Part) -> None:
        if part.handle is not None:
            part.handle.close()
            part.handle = None
        if part.tmp_path is not None:
            _unlink(part.tmp_path)
            part.tmp_path = None

    def finish(self, part: _Part) -> UploadResult:
        """Probe, publish and profile one finished part. Blocking; run in a thread."""
        result = UploadResult(submitted=part.submitted, filename=part.filename)
        try:
            if part.failure is not None:
                raise part.failure
            if part.over_limit is not None:
                raise UploadError(part.over_limit, _limit_detail(self.limits, part.over_limit))
            if part.handle is None or part.tmp_path is None:  # pragma: no cover - defensive
                raise UploadError("write_failed", "the upload produced no data")

            part.handle.flush()
            os.fsync(part.handle.fileno())
            part.handle.close()
            part.handle = None
            if part.written == 0:
                raise UploadError("unsupported_content", "the file is empty")

            target = self._settle(part)
            validate_content(part.tmp_path, part.head, part.suffix, target.kind)
            os.chmod(part.tmp_path, 0o644)
            destination = resolve_destination(target, str(part.filename))
            final, renamed = publish(
                part.tmp_path, destination, rename_on_conflict=self.rename_on_conflict
            )
            part.tmp_path = None
            result.ok = True
            result.filename = final.name
            result.path = self._relative(final)
            result.bytes = part.written
            result.renamed = renamed
            result.profile = self._profile(final, target)
        except UploadError as exc:
            result.error = exc.error
            result.detail = exc.detail
        except OSError as exc:
            result.error = "write_failed"
            result.detail = str(exc)
        finally:
            self._discard(part)
        self.results.append(result)
        return result

    def _relative(self, path: Path) -> str:
        if self.repo_root is not None:
            try:
                return path.relative_to(self.repo_root).as_posix()
            except ValueError:
                pass
        return str(path)

    def _profile(self, image: Path, target: UploadTarget) -> dict[str, Any] | None:
        """A profile describes the image, so it lands beside the tree it is in."""
        if target.kind is not MediaKind.IMAGE or self.repo_root is None:
            return None
        from . import colorprofile

        try:
            profile, _wrote = colorprofile.ensure_profile(
                image, target.tree_root, repo_root=self.repo_root
            )
        except (colorprofile.ProfileError, OSError) as exc:
            LOG.warning("color profile for %s failed: %s", image.name, exc)
            return None
        return colorprofile.summarise(profile)

    def cleanup(self) -> None:
        """Leave nothing behind when a request is abandoned mid-body."""
        for part in [*self.pending, self._part]:
            self._discard(part)
        self.pending.clear()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _limit_error(limits: UploadLimits, budget: int) -> str:
    return "file_too_large" if budget >= limits.max_file_bytes else "insufficient_space"


def _limit_detail(limits: UploadLimits, error: str) -> str:
    if error == "file_too_large":
        return f"the file exceeds the {limits.max_file_bytes} byte limit"
    return f"writing it would leave less than {limits.min_free_bytes} bytes free"


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def boundary_of(content_type: str) -> bytes:
    from python_multipart.multipart import parse_options_header

    _kind, options = parse_options_header(content_type or "")
    if _kind.lower() != b"multipart/form-data":
        raise UploadAborted("invalid_request", "expected a multipart/form-data body")
    boundary = options.get(b"boundary")
    if not boundary:
        raise UploadAborted("invalid_request", "the multipart body has no boundary")
    return boundary


Finalizer = Callable[["UploadReceiver", "_Part"], Awaitable[None]]


async def receive(
    stream: AsyncIterator[bytes],
    content_type: str,
    receiver: UploadReceiver,
    *,
    finalize: Finalizer | None = None,
) -> list[UploadResult]:
    """Parse the body, finalizing each part as soon as it completes."""
    from python_multipart.multipart import MultipartParser

    boundary = boundary_of(content_type)
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": receiver.on_part_begin,
            "on_part_data": receiver.on_part_data,
            "on_part_end": receiver.on_part_end,
            "on_header_field": receiver.on_header_field,
            "on_header_value": receiver.on_header_value,
            "on_header_end": receiver.on_header_end,
            "on_headers_finished": receiver.on_headers_finished,
            "on_end": receiver.on_end,
        },
    )
    run = finalize or _inline_finalize

    try:
        async for chunk in stream:
            parser.write(chunk)
            if receiver.finalize_early:
                await _drain(receiver, run)
        parser.finalize()
        await _drain(receiver, run)
    except UploadAborted:
        receiver.cleanup()
        raise
    except Exception as exc:
        receiver.cleanup()
        LOG.warning("multipart upload failed: %s", exc, exc_info=True)
        raise UploadAborted("malformed_upload", f"the multipart body is malformed: {exc}") from exc
    return receiver.results


async def _drain(receiver: UploadReceiver, run: Finalizer) -> None:
    while receiver.pending:
        await run(receiver, receiver.pending.pop(0))


async def _inline_finalize(receiver: UploadReceiver, part: _Part) -> None:
    receiver.finish(part)


def relative_dir(directory: Path, repo_root: Path) -> str:
    try:
        return Path(directory).relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return str(directory)
