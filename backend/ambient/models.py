"""Pydantic models for the four configuration surfaces.

See docs/contracts/config.md. The split rule: a setting the backend can apply
to a running channel lives in YAML, anything needing a container recreated
lives in ``.env``, and secrets always live in ``.env``.
"""

from __future__ import annotations

import re
from datetime import date as _date
from enum import Enum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

# A channel name becomes a directory name, a Docker Compose project name and an
# Icecast mount, so it is validated as hostile input everywhere it is accepted.
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"
TIME_WINDOW = r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"
MOUNT = r"^/[A-Za-z0-9][A-Za-z0-9._-]*$"
MEMORY_LIMIT = r"^\d+(\.\d+)?[bkmgBKMG]?$"


class Resolution(str, Enum):
    R480 = "480p"
    R720 = "720p"
    R1080 = "1080p"
    R1440 = "1440p"
    R2160 = "2160p"


class Encoder(str, Enum):
    LIBX264 = "libx264"
    NVENC = "h264_nvenc"
    QSV = "h264_qsv"


class ImageOrder(str, Enum):
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"


class ColorMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class BumperMode(str, Enum):
    TRACKS = "tracks"
    TIME = "time"
    BOTH = "both"


class Weekday(str, Enum):
    MON = "Mon"
    TUE = "Tue"
    WED = "Wed"
    THU = "Thu"
    FRI = "Fri"
    SAT = "Sat"
    SUN = "Sun"


class ChannelState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"


class Health(str, Enum):
    HEALTHY = "healthy"
    STARVING = "starving"
    STALLED = "stalled"
    DISCONNECTED = "disconnected"


class StrictModel(BaseModel):
    """YAML surfaces reject unknown keys: a typo must not be silently ignored."""

    model_config = ConfigDict(extra="forbid")


class EnvModel(BaseModel):
    """``.env`` surfaces ignore unrelated keys and treat empty as unset."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and not v.strip())}
        return data


# --------------------------------------------------------------------------
# ambient.yaml
# --------------------------------------------------------------------------


class SlideshowDefaults(StrictModel):
    producer_fps: int = Field(10, ge=1, le=30)
    hold_seconds: float = Field(20.0, gt=0)
    fade_seconds: float = Field(2.0, ge=0)
    jpeg_quality: int = Field(88, ge=1, le=100)

    @model_validator(mode="after")
    def _fade_fits_hold(self) -> SlideshowDefaults:
        if self.fade_seconds >= self.hold_seconds:
            raise ValueError("slideshow.fade_seconds must be shorter than hold_seconds")
        return self


class Defaults(StrictModel):
    resolution: Resolution = Resolution.R720
    fps: int = Field(30, ge=1, le=60)
    encoder: Encoder = Encoder.LIBX264
    crossfade_seconds: float = Field(5.0, ge=0)
    slideshow: SlideshowDefaults = Field(default_factory=SlideshowDefaults)


class Limits(StrictModel):
    max_channels: int = Field(8, ge=1)
    reserved_cores: float = Field(1.0, ge=0)


class Encoders(StrictModel):
    probe_order: list[Encoder] = Field(
        default_factory=lambda: [Encoder.NVENC, Encoder.QSV, Encoder.LIBX264]
    )
    fallback: Encoder = Encoder.LIBX264

    @model_validator(mode="after")
    def _fallback_probed(self) -> Encoders:
        if self.fallback not in self.probe_order:
            raise ValueError("encoders.fallback must appear in encoders.probe_order")
        return self


class Paths(StrictModel):
    common: str = "./common"
    channels: str = "./channels"
    logs: str = "/var/log/ambient"
    # channels/example/ ships as a template; it is not a stream.
    ignore_channels: list[str] = Field(default_factory=lambda: ["example"])


class Relay(StrictModel):
    rtmp: str = "rtmp://mediamtx:1935"
    hls: str = "http://mediamtx:8888"
    icecast: str = "http://icecast:8081"


class WatchdogSettings(StrictModel):
    poll_seconds: float = Field(5.0, gt=0)
    min_speed: float = Field(0.97, gt=0, le=2.0)
    stall_seconds: float = Field(15.0, gt=0)
    restart_backoff_seconds: list[float] = Field(default_factory=lambda: [5, 15, 45, 120, 300])

    @field_validator("restart_backoff_seconds")
    @classmethod
    def _ascending_and_nonempty(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("watchdog.restart_backoff_seconds must not be empty")
        if any(v <= 0 for v in value):
            raise ValueError("watchdog.restart_backoff_seconds values must be positive")
        if list(value) != sorted(value):
            raise ValueError("watchdog.restart_backoff_seconds must ascend (exponential backoff)")
        return value


class Uploads(StrictModel):
    """Limits for operator media upload, enforced while the body streams."""

    max_file_mb: int = Field(512, ge=1)
    max_request_mb: int = Field(2048, ge=1)
    max_files: int = Field(64, ge=1)
    # A 24/7 streamer that fills its own disk takes the stream down.
    min_free_mb: int = Field(1024, ge=0)

    @model_validator(mode="after")
    def _request_fits_one_file(self) -> Uploads:
        if self.max_request_mb < self.max_file_mb:
            raise ValueError("uploads.max_request_mb must be at least uploads.max_file_mb")
        return self


class AmbientConfig(StrictModel):
    version: Literal[1] = 1
    defaults: Defaults = Field(default_factory=Defaults)
    limits: Limits = Field(default_factory=Limits)
    encoders: Encoders = Field(default_factory=Encoders)
    paths: Paths = Field(default_factory=Paths)
    relay: Relay = Field(default_factory=Relay)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    uploads: Uploads = Field(default_factory=Uploads)


# --------------------------------------------------------------------------
# channels/<name>/config.yaml
# --------------------------------------------------------------------------


class AudioSelection(StrictModel):
    tracks: list[str] = Field(default_factory=list)
    shuffle: bool = False
    crossfade_seconds: float | None = Field(None, ge=0)


class ImageSelection(StrictModel):
    slides: list[str] = Field(default_factory=list)
    order: ImageOrder = ImageOrder.SEQUENTIAL
    hold_seconds: float | None = Field(None, gt=0)
    fade_seconds: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _fade_fits_hold(self) -> ImageSelection:
        if self.hold_seconds is not None and self.fade_seconds is not None:
            if self.fade_seconds >= self.hold_seconds:
                raise ValueError("images.fade_seconds must be shorter than hold_seconds")
        return self


class Visualization(StrictModel):
    # Off is by far the cheapest a channel can be. Measured on a live 1080p30
    # channel: 0.77 cores holding 0.999x with it off, against 0.415x — it could
    # not hold realtime at all — with it on. `active` and `hot_set` are kept
    # either way so
    # turning it back on restores the same look.
    enabled: bool = True
    # Standby. `enabled` decides whether the branches exist at all and needs a
    # restart to change; this rides the overlay's timeline `enable` and is one
    # frame. Off here still costs what on costs — the branches are still
    # rendering — which is the price of being able to toggle at all.
    visible: bool = True
    active: str
    hot_set: list[str] = Field(min_length=1)
    # Per plugin, so each keeps its own look across a switch. Values are checked
    # against the plugin manifest, not here: this model has no registry.
    parameters: dict[str, dict[str, float | int | bool | str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _active_is_hot(self) -> Visualization:
        if self.active not in self.hot_set:
            raise ValueError(
                f"visualization.active {self.active!r} is not in hot_set "
                f"{self.hot_set!r}; only instantiated graphs can be switched to"
            )
        if len(set(self.hot_set)) != len(self.hot_set):
            raise ValueError("visualization.hot_set contains duplicates")
        return self


class ManualColor(StrictModel):
    accent: str = Field("#4FC3F7", pattern=HEX_COLOR)
    tint: str = Field("#101820", pattern=HEX_COLOR)


class Color(StrictModel):
    mode: ColorMode = ColorMode.AUTOMATIC
    manual: ManualColor = Field(default_factory=ManualColor)
    transition_seconds: float = Field(2.0, ge=0)


class Bumpers(StrictModel):
    enabled: bool = False
    mode: BumperMode = BumperMode.TRACKS
    every_tracks: int = Field(4, ge=1)
    every_minutes: int = Field(20, ge=1)
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sources_present_when_enabled(self) -> Bumpers:
        if self.enabled and not self.sources:
            raise ValueError("bumpers.sources must be non-empty when bumpers.enabled")
        return self


class ScheduleRule(StrictModel):
    name: str
    preset: str
    when: str | None = Field(None, pattern=TIME_WINDOW)
    days: list[Weekday] | None = None
    date: str | None = None

    @field_validator("date")
    @classmethod
    def _iso_date(cls, value: str | None) -> str | None:
        if value is not None:
            _date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _has_a_selector(self) -> ScheduleRule:
        if self.when is None and self.date is None:
            raise ValueError(f"schedule rule {self.name!r} needs 'when' or 'date'")
        return self


class Schedule(StrictModel):
    timezone: str = "UTC"
    rules: list[ScheduleRule] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone
        return value


class ChannelConfig(StrictModel):
    version: Literal[1] = 1
    name: str
    genre: str = ""
    audio: AudioSelection = Field(default_factory=AudioSelection)
    images: ImageSelection = Field(default_factory=ImageSelection)
    visualization: Visualization
    color: Color = Field(default_factory=Color)
    preset: str | None = None
    bumpers: Bumpers = Field(default_factory=Bumpers)
    schedule: Schedule = Field(default_factory=Schedule)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not CHANNEL_NAME_RE.match(value):
            raise ValueError(
                f"channel name {value!r} must match {CHANNEL_NAME_RE.pattern} "
                "(lowercase, digits, dash, underscore)"
            )
        return value


# --------------------------------------------------------------------------
# .env surfaces
# --------------------------------------------------------------------------


class GlobalEnv(EnvModel):
    data_dir: str = Field("./channels", alias="AMBIENT_DATA_DIR")
    common_dir: str = Field("./common", alias="AMBIENT_COMMON_DIR")
    log_dir: str = Field("/var/log/ambient", alias="AMBIENT_LOG_DIR")
    puid: int = Field(1000, alias="PUID", ge=0)
    pgid: int = Field(1000, alias="PGID", ge=0)
    timezone: str = Field("UTC", alias="TZ")
    backend_port: int = Field(8090, alias="AMBIENT_BACKEND_PORT", ge=1, le=65535)
    icecast_port: int = Field(8081, alias="AMBIENT_ICECAST_PORT", ge=1, le=65535)
    rtmp_port: int = Field(1935, alias="AMBIENT_RTMP_PORT", ge=1, le=65535)
    hls_port: int = Field(8888, alias="AMBIENT_HLS_PORT", ge=1, le=65535)
    hls_publish: int | None = Field(None, alias="AMBIENT_HLS_PUBLISH", ge=1, le=65535)
    bind_address: str = Field("127.0.0.1", alias="AMBIENT_BIND_ADDRESS")
    api_token: SecretStr = Field(SecretStr(""), alias="AMBIENT_API_TOKEN")
    icecast_source_password: SecretStr = Field(SecretStr(""), alias="ICECAST_SOURCE_PASSWORD")
    icecast_admin_password: SecretStr = Field(SecretStr(""), alias="ICECAST_ADMIN_PASSWORD")
    icecast_relay_password: SecretStr = Field(SecretStr(""), alias="ICECAST_RELAY_PASSWORD")
    # None, not a concrete default: these are OVERRIDES layered over ambient.yaml.
    # Giving them a value here would make `defaults:` in ambient.yaml unreachable,
    # because it sits later in the same `or` chain.
    default_encoder: Encoder | None = Field(None, alias="AMBIENT_DEFAULT_ENCODER")
    default_resolution: Resolution | None = Field(None, alias="AMBIENT_DEFAULT_RESOLUTION")
    default_fps: int | None = Field(None, alias="AMBIENT_DEFAULT_FPS", ge=1, le=60)


class ChannelEnv(EnvModel):
    stream_key: SecretStr = Field(SecretStr(""), alias="YOUTUBE_STREAM_KEY")
    rtmp_url: str = Field("rtmp://a.rtmp.youtube.com/live2", alias="YOUTUBE_RTMP_URL")
    resolution: Resolution | None = Field(None, alias="CHANNEL_RESOLUTION")
    fps: int | None = Field(None, alias="CHANNEL_FPS", ge=1, le=60)
    encoder: Encoder | None = Field(None, alias="CHANNEL_ENCODER")
    cpu_limit: float | None = Field(None, alias="CHANNEL_CPU_LIMIT", gt=0)
    memory_limit: str | None = Field(None, alias="CHANNEL_MEMORY_LIMIT", pattern=MEMORY_LIMIT)
    mount: str = Field(..., alias="CHANNEL_MOUNT", pattern=MOUNT)
    fallback_mount: str = Field(..., alias="CHANNEL_FALLBACK_MOUNT", pattern=MOUNT)

    # An internal channel publishes only its local HLS rendition. The relay's
    # program path never goes ready, so the YouTube hook cannot fire — that is
    # structural, not "the stream key happens to be blank", which a paste into
    # the wrong channel would undo.
    publish_youtube: bool = Field(True, alias="CHANNEL_PUBLISH_YOUTUBE")
    # The local HLS rendition. Defaults differ by delivery: a YouTube channel
    # gets an operator-sized preview, an internal channel gets its full size,
    # because for it this is the only output there is.
    local_height: int | None = Field(None, alias="CHANNEL_LOCAL_HEIGHT", ge=144, le=2160)
    local_fps: int | None = Field(None, alias="CHANNEL_LOCAL_FPS", ge=1, le=60)

    @model_validator(mode="after")
    def _mounts_differ(self) -> ChannelEnv:
        if self.mount == self.fallback_mount:
            raise ValueError("CHANNEL_MOUNT and CHANNEL_FALLBACK_MOUNT must differ")
        return self
