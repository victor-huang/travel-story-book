"""Typed configuration.

Every tunable in the pipeline lives here and nowhere else. Stage code must never contain a
magic number -- if a stage needs a threshold, it gets one from this module so that the whole
system can be retuned from a single file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

CONFIG_SCHEMA_VERSION = 1


class ConfigError(Exception):
    """Raised for malformed, unknown, or out-of-range configuration."""


@dataclass(frozen=True, slots=True)
class HomeLocation:
    """Where the user lives, used to keep private media out of exports."""

    lat: float
    lon: float
    exclusion_km: float = 5.0


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Per-device corrections, keyed in TOML by the EXIF model string."""

    clock_offset_minutes: int = 0
    default_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class TimeConfig:
    day_start_hour: int = 4
    suspicious_gap_days: float = 3.0
    default_timezone: str = "UTC"
    gps_interpolation_window_minutes: float = 120.0


@dataclass(frozen=True, slots=True)
class EventConfig:
    gap_minutes: float = 90.0
    jump_km: float = 1.5
    min_items: int = 2

    # The rule that actually breaks up a long day. Neither the time gap nor the GPS jump fires
    # while wandering a city centre for nine hours: shots are minutes apart and every position is
    # within `jump_km` of the running centroid. Measured on a real 141-item day, this single
    # threshold took it from 4 events to 7; nothing else moved the number.
    max_minutes: float = 150.0


@dataclass(frozen=True, slots=True)
class DedupConfig:
    phash_max_distance: int = 6
    burst_max_seconds: float = 3.0
    similar_min_cosine: float = 0.92


@dataclass(frozen=True, slots=True)
class QualityWeights:
    """Weights for the documented overall-score sum. Need not total 1.0; normalized at use."""

    sharpness: float = 0.40
    exposure: float = 0.25
    contrast: float = 0.15
    face: float = 0.20


@dataclass(frozen=True, slots=True)
class QualityConfig:
    weights: QualityWeights = field(default_factory=QualityWeights)
    reject_content_classes: tuple[str, ...] = ("screenshot", "receipt", "document")
    min_overall_for_highlight: float = 0.35


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    highlights_per_event: int = 5
    highlights_per_day: int = 10
    diversity_min_distance: float = 0.15


@dataclass(frozen=True, slots=True)
class VideoConfig:
    transcribe: str = "auto"
    transcribe_min_seconds: float = 10.0
    whisper_model: str = "small"
    keyframe_count: int = 5
    # Whisper hallucinates fluent nonsense on ambient noise and on music. These gates discard a
    # transcript rather than let a fabricated quote reach the travel journal. Values measured
    # against real clips: a concert recording mis-transcribed as speech scored
    # language_probability 0.26 and avg_logprob -0.85, where genuine speech sits near 0.9 / -0.4.
    transcript_min_language_probability: float = 0.5
    transcript_min_avg_logprob: float = -0.7
    transcript_max_no_speech_prob: float = 0.6
    transcript_min_chars: int = 16
    # Mean volume below this is treated as silence, so `transcribe = "auto"` skips the clip.
    # Measured against the fixtures: a clip with a voice-band tone reads ~-21 dB, a silent
    # clip ~-91 dB. -50 sits clear of both.
    speech_mean_volume_floor_db: float = -50.0

    def __post_init__(self) -> None:
        if self.transcribe not in {"none", "auto", "all"}:
            raise ConfigError(
                f"video.transcribe must be one of none/auto/all, got {self.transcribe!r}"
            )


@dataclass(frozen=True, slots=True)
class ModelConfig:
    clip_name: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    clip_batch_size: int = 32
    device: str = "auto"
    # Path to a YuNet ONNX face-detection model. OpenCV 5.x dropped the classic Haar cascade
    # API and ships no detection data, so there is nothing to fall back on. Left unset by
    # default rather than downloaded silently: the project's promise is that only the landmark
    # stage reaches the network. Unset means the face signal is unavailable, and the quality
    # score renormalizes over its remaining components rather than carrying dead weight.
    face_detector_model: str | None = None


@dataclass(frozen=True, slots=True)
class GeocodeConfig:
    use_nominatim: bool = False
    nominatim_user_agent: str = "travel-story-book"
    nominatim_min_interval_seconds: float = 1.1
    coordinate_rounding_decimals: int = 4


@dataclass(frozen=True, slots=True)
class LandmarkConfig:
    provider: str = "none"
    model: str | None = None
    images_per_request: int = 4
    max_requests: int = 400
    confirm_above_estimated_usd: float = 1.0
    prompt_version: int = 1


@dataclass(frozen=True, slots=True)
class Config:
    trip_name: str | None = None
    no_cloud: bool = False
    home: HomeLocation | None = None
    time: TimeConfig = field(default_factory=TimeConfig)
    events: EventConfig = field(default_factory=EventConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    geocode: GeocodeConfig = field(default_factory=GeocodeConfig)
    landmarks: LandmarkConfig = field(default_factory=LandmarkConfig)
    devices: dict[str, DeviceConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> Config:
        if path is None:
            return cls()
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        raw = dict(raw)
        version = raw.pop("config_version", CONFIG_SCHEMA_VERSION)
        if version != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"config_version {version} is not supported (expected {CONFIG_SCHEMA_VERSION})"
            )
        devices = {
            name: _build(DeviceConfig, value, f"devices.{name}")
            for name, value in (raw.pop("devices", None) or {}).items()
        }
        config = _build(cls, raw, "config")
        return replace_devices(config, devices)


def replace_devices(config: Config, devices: dict[str, DeviceConfig]) -> Config:
    """Rebuild a Config with a devices mapping, keeping the dataclass frozen."""
    values = {f.name: getattr(config, f.name) for f in fields(config)}
    values["devices"] = devices
    return Config(**values)


def _build[T](cls: type[T], raw: Any, where: str) -> T:
    """Recursively construct a nested config dataclass, rejecting unknown keys."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table, got {type(raw).__name__}")

    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        options = ", ".join(sorted(known))
        raise ConfigError(
            f"unknown key(s) in {where}: {', '.join(sorted(unknown))}. Valid keys: {options}"
        )

    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        target = _unwrap_optional(known[name].type)
        if is_dataclass(target) and isinstance(value, dict):
            kwargs[name] = _build(target, value, f"{where}.{name}")
        elif isinstance(value, list):
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid {where}: {exc}") from exc


_NESTED_TYPES: dict[str, type] = {
    "HomeLocation": HomeLocation,
    "TimeConfig": TimeConfig,
    "EventConfig": EventConfig,
    "DedupConfig": DedupConfig,
    "QualityConfig": QualityConfig,
    "QualityWeights": QualityWeights,
    "SelectionConfig": SelectionConfig,
    "VideoConfig": VideoConfig,
    "ModelConfig": ModelConfig,
    "GeocodeConfig": GeocodeConfig,
    "LandmarkConfig": LandmarkConfig,
    "DeviceConfig": DeviceConfig,
}


def _unwrap_optional(annotation: Any) -> Any:
    """Resolve a dataclass field annotation, which is a string under future annotations."""
    if isinstance(annotation, type):
        return annotation
    text = str(annotation).replace(" ", "")
    text = text.removesuffix("|None")
    return _NESTED_TYPES.get(text, annotation)
