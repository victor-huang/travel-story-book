"""Inter-stage data contract.

These dataclasses are what stages pass to each other and what they persist. Changing a field
here changes every consumer, so treat this module as frozen once Wave 1 begins -- amend it
only through the tracker's cross-task request process.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class TzSource(StrEnum):
    """How a media item's timezone was determined, best to worst."""

    EXIF_OFFSET = "exif_offset"
    GPS = "gps"
    DEVICE_NEIGHBOR = "device_neighbor"
    CONFIG = "config"
    UNKNOWN = "unknown"


class GpsSource(StrEnum):
    EXIF = "exif"
    INTERPOLATED = "interpolated"
    MANUAL = "manual"
    NONE = "none"


class ClusterKind(StrEnum):
    EXACT = "exact"
    BURST = "burst"
    SIMILAR = "similar"


class SelectionScope(StrEnum):
    CLUSTER = "cluster"
    EVENT = "event"
    DAY = "day"
    TRIP = "trip"


class StageStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class Media:
    """One photo or video, identified by the hash of its bytes."""

    hash: str
    path: str
    kind: MediaKind
    bytes: int
    mtime: float
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    device_id: str | None = None
    taken_local: str | None = None
    taken_utc: str | None = None
    tz_name: str | None = None
    tz_offset_minutes: int | None = None
    tz_source: TzSource = TzSource.UNKNOWN
    exif_offset_minutes: int | None = None
    """The raw `OffsetTimeOriginal` tag, as read. Never overwritten by resolution."""
    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None
    gps_source: GpsSource = GpsSource.NONE
    gps_confidence: float | None = None
    place_id: int | None = None
    is_near_home: bool = False

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Media:
        return cls(
            hash=row["hash"],
            path=row["path"],
            kind=MediaKind(row["kind"]),
            bytes=row["bytes"],
            mtime=row["mtime"],
            width=row["width"],
            height=row["height"],
            duration=row["duration"],
            device_id=row["device_id"],
            taken_local=row["taken_local"],
            taken_utc=row["taken_utc"],
            tz_name=row["tz_name"],
            tz_offset_minutes=row["tz_offset_minutes"],
            tz_source=TzSource(row["tz_source"] or TzSource.UNKNOWN),
            exif_offset_minutes=row["exif_offset_minutes"],
            lat=row["lat"],
            lon=row["lon"],
            altitude=row["altitude"],
            gps_source=GpsSource(row["gps_source"] or GpsSource.NONE),
            gps_confidence=row["gps_confidence"],
            place_id=row["place_id"],
            is_near_home=bool(row["is_near_home"]),
        )


@dataclass(slots=True)
class Device:
    id: str
    make: str | None = None
    model: str | None = None
    clock_offset_minutes: int = 0


@dataclass(slots=True)
class Place:
    id: int | None
    lat_key: float
    lon_key: float
    source: str
    poi: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None

    @property
    def label(self) -> str:
        parts = [p for p in (self.poi, self.city, self.country) if p]
        return ", ".join(parts) if parts else "Unknown location"


@dataclass(slots=True)
class Trip:
    name: str
    id: int = 1
    start_local: str | None = None
    end_local: str | None = None
    home_lat: float | None = None
    home_lon: float | None = None


@dataclass(slots=True)
class Day:
    id: int | None
    trip_id: int
    local_date: str


@dataclass(slots=True)
class Event:
    id: int | None
    day_id: int
    seq: int
    start_utc: str | None = None
    end_utc: str | None = None
    centroid_lat: float | None = None
    centroid_lon: float | None = None
    place_id: int | None = None
    label: str | None = None


@dataclass(slots=True)
class Cluster:
    id: int | None
    event_id: int
    kind: ClusterKind
    keeper_hash: str | None = None


@dataclass(slots=True)
class Score:
    media_hash: str
    sharpness: float | None = None
    exposure: float | None = None
    contrast: float | None = None
    face_count: int | None = None
    face_max_frac: float | None = None
    content_class: str | None = None
    overall: float | None = None


@dataclass(slots=True)
class Embedding:
    media_hash: str
    model: str
    dim: int
    vector: bytes


@dataclass(slots=True)
class Landmark:
    id: int | None
    name: str
    source: str
    confidence: float | None = None
    description: str | None = None
    prompt_version: int = 1


@dataclass(slots=True)
class Transcript:
    media_hash: str
    model: str
    text: str
    segments: str | None = None


@dataclass(slots=True)
class Selection:
    media_hash: str
    scope: SelectionScope
    scope_id: int
    rank: int
    reason: str | None = None


@dataclass(slots=True)
class StageResult:
    media_hash: str
    stage: str
    stage_version: int
    status: StageStatus
    computed_at: str
    error: str | None = None
