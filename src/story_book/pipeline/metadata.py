"""Module 2: metadata extraction.

Populates `taken_local`, `width`, `height`, `duration`, `device_id`, `lat`, `lon`, `altitude`,
and `gps_source` from EXIF, plus `device` rows for make/model. Does **not** compute timezones or
`taken_utc` -- that is T12's job, and `tz_*` fields are left untouched here.

Batched via `exiftool` (see `story_book.exif`) -- one process per chunk of files, never one per
file, and never `-fast2`. Missing or garbage EXIF degrades to null fields; it never raises.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from story_book.db import connection as db
from story_book.db.models import GpsSource, Media
from story_book.exif import exiftool_available, extract_timestamp, run_exiftool
from story_book.pipeline.base import BatchStage, StageContext


def _device_id(make: str | None, model: str | None) -> str | None:
    parts = [p.strip() for p in (make, model) if p and p.strip()]
    return " ".join(parts) or None


def _upsert_device(
    conn: sqlite3.Connection, device_id: str, make: str | None, model: str | None
) -> None:
    """Insert or update a `device` row. Not covered by `db.connection` helpers, so this is the
    one place in this module that writes SQL directly -- `device`, unlike `media` and
    `stage_result`, has no shared upsert helper yet."""
    conn.execute(
        """
        INSERT INTO device (id, make, model) VALUES (?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET make = excluded.make, model = excluded.model
        """,
        (device_id, make, model),
    )


def _as_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_int(value: object) -> int | None:
    number = _as_number(value)
    return int(number) if number is not None else None


class MetadataStage(BatchStage):
    """Extracts EXIF metadata for every media item, batched through one exiftool process
    per chunk."""

    name = "metadata"
    version = 1
    description = "EXIF metadata extraction (timestamps, dimensions, GPS, device)"

    # A chunk per the binding P01 finding: ~200-500 files per exiftool process. Also bounds how
    # many items get re-attempted together if a batch fails outright.
    batch_size = 300

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        if not exiftool_available():
            return False, "exiftool binary not found on PATH"
        return True, ""

    def select(self, ctx: StageContext) -> list[Media]:
        return list(db.iter_media(ctx.conn))

    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        paths = [Path(media.path) for media in batch]
        raw = run_exiftool(paths)

        results: dict[str, Any] = {}
        for media in batch:
            meta = raw.get(str(Path(media.path))) or {}
            self._apply(ctx.conn, media, meta)
            results[media.hash] = True
        return results

    def _apply(self, conn: sqlite3.Connection, media: Media, meta: dict) -> None:
        timestamp = extract_timestamp(meta, media.kind)
        media.taken_local = timestamp.dt.isoformat() if timestamp.dt is not None else None

        media.width = _as_int(meta.get("ImageWidth"))
        media.height = _as_int(meta.get("ImageHeight"))
        media.duration = _as_number(meta.get("Duration"))

        make = (meta.get("Make") or "").strip() or None
        model = (meta.get("Model") or "").strip() or None
        device_id = _device_id(make, model)
        if device_id is not None:
            _upsert_device(conn, device_id, make, model)
        media.device_id = device_id

        media.lat = _as_number(meta.get("GPSLatitude"))
        media.lon = _as_number(meta.get("GPSLongitude"))
        media.altitude = _as_number(meta.get("GPSAltitude"))
        media.gps_source = GpsSource.EXIF if media.has_gps else GpsSource.NONE

        db.upsert_media(conn, media)
