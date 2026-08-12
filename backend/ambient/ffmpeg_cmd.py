"""Composer command assembly.

Encoder profiles and the bitrate ladder come from the youtube-ingest skill;
the mandatory input flags come from docs/contracts/audio-transport.md. Both
were proven in production and are not re-derived here.

Encoder availability is decided by a real test encode. `ffmpeg -encoders`
advertises h264_qsv on hosts with no Intel device and h264_nvenc on hosts with
a broken driver, so the list alone is not evidence.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Sequence

from .models import Encoder, Resolution

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "144p": (256, 144),
    "240p": (426, 240),
    "360p": (640, 360),
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}

# video kbps, bufsize kbps (always 2x video), audio kbps
LADDER: dict[str, tuple[int, int, int]] = {
    "144p": (400, 800, 96),
    "240p": (700, 1400, 96),
    "360p": (1000, 2000, 128),
    "480p": (1500, 3000, 128),
    "720p": (3000, 6000, 128),
    "1080p": (5000, 10000, 192),
    "1440p": (8000, 16000, 192),
    "2160p": (16000, 32000, 256),
}

PREVIEW_RESOLUTION = "360p"

# Icecast can only be probed once a source has logged in, and compose starts
# both containers at once — see audio-transport.md.
AUDIO_INPUT_FLAGS: tuple[str, ...] = (
    "-probesize", "32k",
    "-analyzeduration", "500000",
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_delay_max", "5",
)

AUDIO_FILTER_CHAIN = "aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11"


class EncoderUnavailable(RuntimeError):
    """No configured encoder survived its probe."""


def _name(value: Resolution | str) -> str:
    return value.value if isinstance(value, Resolution) else str(value)


def geometry(resolution: Resolution | str) -> tuple[int, int]:
    key = _name(resolution)
    try:
        return RESOLUTIONS[key]
    except KeyError as exc:
        raise ValueError(f"unknown resolution {key!r}") from exc


@dataclass(frozen=True)
class Rates:
    video_kbps: int
    bufsize_kbps: int
    audio_kbps: int


def rates(resolution: Resolution | str, fps: int = 30) -> Rates:
    key = _name(resolution)
    try:
        video, bufsize, audio = LADDER[key]
    except KeyError as exc:
        raise ValueError(f"unknown resolution {key!r}") from exc
    if fps >= 50:
        video = int(round(video * 1.5))
        bufsize = video * 2
    return Rates(video_kbps=video, bufsize_kbps=bufsize, audio_kbps=audio)


def _encoder_specific(encoder: Encoder | str) -> list[str]:
    value = encoder.value if isinstance(encoder, Encoder) else str(encoder)
    if value == Encoder.LIBX264.value:
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-sc_threshold", "0",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
        ]
    if value == Encoder.NVENC.value:
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-tune", "ll",
            "-rc", "cbr",
            "-cbr", "1",
            "-no-scenecut", "1",
        ]
    if value == Encoder.QSV.value:
        return ["-c:v", "h264_qsv", "-preset", "medium", "-rc_mode", "CBR"]
    raise ValueError(f"unknown encoder {value!r}")


def video_flags(encoder: Encoder | str, fps: int, rate: Rates) -> list[str]:
    """CBR at the ladder rate with a fixed 2-second GOP — what YouTube expects."""
    gop = fps * 2
    bitrate = f"{rate.video_kbps}k"
    return [
        *_encoder_specific(encoder),
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-b:v", bitrate,
        "-minrate", bitrate,
        "-maxrate", bitrate,
        "-bufsize", f"{rate.bufsize_kbps}k",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-pix_fmt", "yuv420p",
    ]


def audio_flags(rate: Rates) -> list[str]:
    return ["-c:a", "aac", "-b:a", f"{rate.audio_kbps}k", "-ar", "44100"]


# --------------------------------------------------------------------------
# Encoder probing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    encoder: str
    available: bool
    detail: str = ""


_PROBE_CACHE: dict[tuple[str, str], ProbeResult] = {}


def probe_command(encoder: Encoder | str, ffmpeg: str = "ffmpeg") -> list[str]:
    rate = Rates(video_kbps=500, bufsize_kbps=1000, audio_kbps=128)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-f", "lavfi",
        "-i", "color=c=black:s=320x240:r=30",
        "-frames:v", "15",
        *video_flags(encoder, 30, rate),
        "-f", "null",
        "-",
    ]


def probe_encoder(
    encoder: Encoder | str,
    ffmpeg: str = "ffmpeg",
    timeout: float = 60.0,
    use_cache: bool = True,
) -> ProbeResult:
    """Run a real short encode. The encoder list is not evidence."""
    key = (_name_of(encoder), ffmpeg)
    if use_cache and key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    try:
        completed = subprocess.run(
            probe_command(encoder, ffmpeg),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        result = ProbeResult(key[0], False, f"{ffmpeg} not found")
    except subprocess.TimeoutExpired:
        result = ProbeResult(key[0], False, f"probe timed out after {timeout:.0f}s")
    else:
        if completed.returncode == 0:
            result = ProbeResult(key[0], True)
        else:
            detail = (completed.stderr or "").strip().splitlines()
            result = ProbeResult(key[0], False, detail[-1] if detail else f"rc={completed.returncode}")
    _PROBE_CACHE[key] = result
    return result


def _name_of(encoder: Encoder | str) -> str:
    return encoder.value if isinstance(encoder, Encoder) else str(encoder)


def probe_encoders(
    candidates: Sequence[Encoder | str], ffmpeg: str = "ffmpeg", use_cache: bool = True
) -> dict[str, ProbeResult]:
    return {_name_of(c): probe_encoder(c, ffmpeg, use_cache=use_cache) for c in candidates}


@dataclass(frozen=True)
class EncoderSelection:
    encoder: str
    requested: str
    substituted: bool
    reason: str = ""


def select_encoder(
    requested: Encoder | str,
    probe_order: Sequence[Encoder | str],
    fallback: Encoder | str,
    ffmpeg: str = "ffmpeg",
    use_cache: bool = True,
) -> EncoderSelection:
    """A channel whose encoder fails its probe starts on the fallback and
    reports the substitution rather than failing to start."""
    wanted = _name_of(requested)
    result = probe_encoder(requested, ffmpeg, use_cache=use_cache)
    if result.available:
        return EncoderSelection(wanted, wanted, False)

    tried = [f"{wanted}: {result.detail}"]
    order = [_name_of(fallback)] + [
        _name_of(c) for c in probe_order if _name_of(c) not in (wanted, _name_of(fallback))
    ]
    for candidate in order:
        probe = probe_encoder(candidate, ffmpeg, use_cache=use_cache)
        if probe.available:
            return EncoderSelection(
                candidate, wanted, True, f"{wanted} failed its probe ({result.detail})"
            )
        tried.append(f"{candidate}: {probe.detail}")
    raise EncoderUnavailable("no encoder passed a test encode — " + "; ".join(tried))


# --------------------------------------------------------------------------
# Command assembly
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewOutput:
    """Second output to the relay; MediaMTX does not transcode."""

    video_label: str
    rtmp_url: str
    resolution: str = PREVIEW_RESOLUTION
    fps: int = 15


@dataclass
class ComposerSpec:
    channel: str
    resolution: Resolution | str
    fps: int
    encoder: Encoder | str
    icecast_url: str
    filter_complex: str
    rtmp_url: str
    stream_key: str
    producer_fps: int = 10
    video_label: str = "vout"
    audio_label: str = "aout"
    preview: PreviewOutput | None = None
    progress_path: str | None = None
    log_level: str = "warning"
    extra_input_args: Sequence[str] = field(default_factory=tuple)
    extra_output_args: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ComposerCommand:
    argv: list[str]
    secrets: tuple[str, ...] = ()

    @property
    def redacted(self) -> list[str]:
        out = []
        for arg in self.argv:
            for secret in self.secrets:
                if secret and secret in arg:
                    arg = arg.replace(secret, "<redacted>")
            out.append(arg)
        return out

    def __str__(self) -> str:
        return shlex.join(self.redacted)

    def __repr__(self) -> str:  # a stream key must never reach a log
        return f"ComposerCommand({shlex.join(self.redacted)})"


def _publish_url(base: str, key: str) -> str:
    return f"{base.rstrip('/')}/{key}" if key else base.rstrip("/")


def build_composer_command(spec: ComposerSpec) -> ComposerCommand:
    """Assemble the compositor command line.

    `filter_complex` is supplied by the caller — the filtergraph belongs to the
    media-pipeline lane. This function owns the inputs, the encode ladder and
    the outputs.
    """
    if not spec.filter_complex.strip():
        raise ValueError("filter_complex must not be empty")

    rate = rates(spec.resolution, spec.fps)
    argv: list[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", spec.log_level]
    if spec.progress_path:
        argv += ["-progress", spec.progress_path, "-stats_period", "0.5"]

    argv += [*AUDIO_INPUT_FLAGS, *spec.extra_input_args, "-i", spec.icecast_url]
    argv += ["-f", "image2pipe", "-framerate", str(spec.producer_fps), "-i", "pipe:0"]
    argv += ["-filter_complex", spec.filter_complex]

    argv += ["-map", f"[{spec.video_label}]", "-map", f"[{spec.audio_label}]"]
    argv += video_flags(spec.encoder, spec.fps, rate)
    argv += audio_flags(rate)
    argv += [*spec.extra_output_args, "-f", "flv", _publish_url(spec.rtmp_url, spec.stream_key)]

    if spec.preview is not None:
        preview_rate = rates(spec.preview.resolution, spec.preview.fps)
        argv += ["-map", f"[{spec.preview.video_label}]", "-map", f"[{spec.audio_label}]"]
        argv += video_flags(Encoder.LIBX264, spec.preview.fps, preview_rate)
        argv += audio_flags(preview_rate)
        argv += ["-f", "flv", spec.preview.rtmp_url]

    return ComposerCommand(argv=argv, secrets=(spec.stream_key,) if spec.stream_key else ())
