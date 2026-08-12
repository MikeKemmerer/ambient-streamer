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

HEX_COLOUR = r"^#[0-9A-Fa-f]{6}$"
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


class ColourMode(str, Enum):
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


class AmbientConfig(StrictModel):
    version: Literal[1] = 1
    defaults: Defaults = Field(default_factory=Defaults)
    limits: Limits = Field(default_factory=Limits)
    encoders: Encoders = Field(default_factory=Encoders)
    paths: Paths = Field(default_factory=Paths)
    relay: Relay = Field(default_factory=Relay)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)


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


class Visualisation(StrictModel):
    active: str
    hot_set: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _active_is_hot(self) -> Visualisation:
        if self.active not in self.hot_set:
            raise ValueError(
                f"visualisation.active {self.active!r} is not in hot_set "
                f"{self.hot_set!r}; only instantiated graphs can be switched to"
            )
        if len(set(self.hot_set)) != len(self.hot_set):
            raise ValueError("visualisation.hot_set contains duplicates")
        return self


class ManualColour(StrictModel):
    accent: str = Field("#4FC3F7", pattern=HEX_COLOUR)
    tint: str = Field("#101820", pattern=HEX_COLOUR)


class Colour(StrictModel):
    mode: ColourMode = ColourMode.AUTOMATIC
    manual: ManualColour = Field(default_factory=ManualColour)
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
    visualisation: Visualisation
    colour: Colour = Field(default_factory=Colour)
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
    default_encoder: Encoder = Field(Encoder.LIBX264, alias="AMBIENT_DEFAULT_ENCODER")
    default_resolution: Resolution = Field(Resolution.R720, alias="AMBIENT_DEFAULT_RESOLUTION")
    default_fps: int = Field(30, alias="AMBIENT_DEFAULT_FPS", ge=1, le=60)


class ChannelEnv(EnvModel):
    stream_key: SecretStr = Field(SecretStr(""), alias="YOUTUBE_STREAM_KEY")
    rtmp_url: str = Field("rtmp://a.rtmp.youtube.com/live2", alias="YOUTUBE_RTMP_URL")
    rtmp_backup_url: str | None = Field(None, alias="YOUTUBE_RTMP_BACKUP_URL")
    resolution: Resolution | None = Field(None, alias="CHANNEL_RESOLUTION")
    fps: int | None = Field(None, alias="CHANNEL_FPS", ge=1, le=60)
    encoder: Encoder | None = Field(None, alias="CHANNEL_ENCODER")
    cpu_limit: float | None = Field(None, alias="CHANNEL_CPU_LIMIT", gt=0)
    memory_limit: str | None = Field(None, alias="CHANNEL_MEMORY_LIMIT", pattern=MEMORY_LIMIT)
    mount: str = Field(..., alias="CHANNEL_MOUNT", pattern=MOUNT)
    fallback_mount: str = Field(..., alias="CHANNEL_FALLBACK_MOUNT", pattern=MOUNT)

    @model_validator(mode="after")
    def _mounts_differ(self) -> ChannelEnv:
        if self.mount == self.fallback_mount:
            raise ValueError("CHANNEL_MOUNT and CHANNEL_FALLBACK_MOUNT must differ")
        return self
