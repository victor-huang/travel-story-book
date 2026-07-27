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

        # Hand the raw EXIF offset to T12 through the two fields the frozen Media model has for
        # it. Without this the offset was parsed and discarded, tz_source stayed UNKNOWN, and
        # level 1 of the timezone resolution order (validated EXIF offset) could never fire --
        # every item silently fell through to GPS. TimezoneStage re-validates this against GPS
        # and overrides it on disagreement; recording it is not the same as trusting it.
        # Into its own column, not the resolved ones. Timezone resolution reads this and writes
        # tz_*; sharing a column meant that stage overwrote its own input and a re-run produced a
        # different, worse answer than the first run.
        media.exif_offset_minutes = timestamp.offset_minutes

        media.width = _as_int(meta.get("ImageWidth"))
        media.height = _as_int(meta.get("ImageHeight"))
        media.duration = _as_number(meta.get("Duration"))

        make = (meta.get("Make") or "").strip() or None
        model = (meta.get("Model") or "").strip() or None
        device_id = _device_id(make, model)
        if device_id is not None:
            db.upsert_device(conn, device_id, make, model)
        media.device_id = device_id

        media.lat = _as_number(meta.get("GPSLatitude"))
        media.lon = _as_number(meta.get("GPSLongitude"))
        media.altitude = _as_number(meta.get("GPSAltitude"))
        media.gps_source = GpsSource.EXIF if media.has_gps else GpsSource.NONE

        db.upsert_media(conn, media)
