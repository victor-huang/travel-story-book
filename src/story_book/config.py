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


@dataclass(frozen=True, slots=True)
class DedupConfig:
    """Both signals must agree before two photos are called duplicates -- see `classify_pair`."""

    # pHash *proposes*. Distances over 11,709 real within-event pairs centre on 31.3 (spread 4.2),
    # with 12 pairs at <=14, 19 at <=16, 27 at <=18 and 100 at <=20, so 18 sits just under the
    # noise floor. The original guess of 6 caught one of nine real duplicates.
    phash_max_distance: int = 18

    burst_max_seconds: float = 3.0

    # CLIP *confirms*. pHash sees low-frequency structure, so it happily merges two different
    # composer busts lit identically, or two different paintings in matching frames -- all at
    # Hamming 14-18. At that ambiguous band real duplicates scored 0.931-0.952 while those false
    # merges scored 0.838 and 0.625, which is a clean gap.
    #
    # Note this threshold is only meaningful *given* a pHash match. CLIP on its own cannot separate
    # the classes at all (duplicates 0.836-0.956, distinct 0.838-0.929), which is why the plan's
    # original "pHash OR CLIP" design fails and conjunction is used instead.
    confirm_min_cosine: float = 0.90


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
class TimelineConfig:
    """`trip.json`, the artifact both outputs render from."""

    # A prefix of the BLAKE2b content hash, used as the stable public id for a photo.
    # Contact-sheet cell ids are positional and change whenever selection changes, so they cannot
    # be an identity; this can. 16 hex digits is 64 bits: 8 was enough for one trip but only 32
    # bits, and these ids are meant to survive into a library. The builder lengthens the prefix
    # further rather than ever emitting a duplicate, so a collision is impossible by construction
    # -- the length only controls how often that lengthening has to happen.
    asset_id_length: int = 16

    # Douglas-Peucker tolerance for an event's walking path. Raw paths are one point per photo,
    # which is 121 points for a single afternoon and mostly camera jitter at one spot.
    path_simplify_meters: float = 25.0

    # Below this end-to-end span an event is a *stop*, not a walk, and gets no path -- a
    # scattering of points around one courtyard is noise, not movement.
    path_min_span_meters: float = 100.0


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """Derived images and the static HTML report."""

    # Long edge in pixels. The thumbnail is a contact-sheet/grid cell; the preview is what a
    # reader opens full-screen and what the package ships when it is not shipping originals.
    thumbnail_long_edge: int = 480
    preview_long_edge: int = 1600
    jpeg_quality: int = 82

    # Photos per day page before the gallery paginates. Purely presentational.
    gallery_page_size: int = 240


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
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
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
    "TimelineConfig": TimelineConfig,
    "ReportConfig": ReportConfig,
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
