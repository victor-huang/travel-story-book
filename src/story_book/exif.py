"""Canonical EXIF field-priority and timestamp/offset parsing.

Shared home for logic that two components must agree on: `pipeline/metadata.py` (T11) uses it
for real extraction and persistence, and `profile.py` (T17) is meant to be migrated onto it so
the two stop being able to silently disagree about which field wins.

Two binding findings from a real-data profiling pass (`dev_plan/p01_profile_findings.md`),
amending Module 2 of the plan doc:

1. Batch ExifTool -- one process per chunk of ~200-500 files, fed over stdin with `-@ -` so an
   8,000-file folder can't blow past `ARG_MAX`. Per-file spawn is a ~20x slowdown.
2. Never pass `-fast2`. It skips the moov atom and silently zeroes video `Duration` with no
   error -- the absent field just becomes `0.0`.
3. Timestamp field priority differs by media kind. On Photos-exported `.mov`,
   `CreateDate`/`MediaCreateDate`/every `Track*CreateDate` hold the *export* time; only
   `QuickTime:Keys:CreationDate` holds the real capture time, and it carries the original UTC
   offset.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from story_book.db.models import MediaKind

# Field priority, highest-trust first. Recorded per item so a video that fell back to
# CreateDate/MediaCreateDate can be flagged as a probable export artifact.
VIDEO_FIELD_PRIORITY: tuple[str, ...] = (
    "CreationDate",
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
)
IMAGE_FIELD_PRIORITY: tuple[str, ...] = (
    "DateTimeOriginal",
    "CreationDate",
    "CreateDate",
    "MediaCreateDate",
)

# Fields whose presence on a video means the timestamp is probably when the file was
# exported/re-encoded, not when it was captured.
VIDEO_EXPORT_ARTIFACT_FIELDS: frozenset[str] = frozenset({"CreateDate", "MediaCreateDate"})

# exiftool arguments requested for every item. `-n` gives numeric (not human-formatted) output,
# which is what makes GPS coordinates and Duration come back as plain signed numbers.
EXIFTOOL_REQUEST_FIELDS: tuple[str, ...] = (
    "-SourceFile",
    "-Make",
    "-Model",
    "-DateTimeOriginal",
    "-OffsetTimeOriginal",
    "-CreationDate",
    "-CreateDate",
    "-MediaCreateDate",
    "-GPSLatitude",
    "-GPSLongitude",
    "-GPSAltitude",
    "-ImageWidth",
    "-ImageHeight",
    "-Duration",
)

# One exiftool process per chunk of files -- never one per file (~20x slowdown), and never the
# whole run's file list at once (risks ARG_MAX -- mitigated further by feeding paths on stdin).
DEFAULT_CHUNK_SIZE = 400


def field_priority(kind: MediaKind) -> tuple[str, ...]:
    """Which EXIF field to trust first for a timestamp, given the media kind."""
    return VIDEO_FIELD_PRIORITY if kind is MediaKind.VIDEO else IMAGE_FIELD_PRIORITY


def parse_exif_datetime(value: object) -> datetime | None:
    """EXIF stamps look like '2026:07:18 09:20:00', sometimes with a trailing offset."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(("0000", "    ")):
        return None
    text = text.split("+")[0].split("Z")[0].strip()
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
    return None


def parse_offset(value: object) -> int | None:
    """'+02:00' -> 120 minutes. Returns None for anything that doesn't parse."""
    if not isinstance(value, str) or len(value) < 6 or value[0] not in "+-":
        return None
    try:
        hours, minutes = int(value[1:3]), int(value[4:6])
    except ValueError:
        return None
    total = hours * 60 + minutes
    return -total if value[0] == "-" else total


def embedded_offset(value: object) -> int | None:
    """Offset carried inside a timestamp string, e.g. '2026:07:18 11:37:58+02:00'."""
    if not isinstance(value, str) or len(value) < 25:
        return None
    return parse_offset(value[19:25])


@dataclass(slots=True)
class ExifTimestamp:
    """The result of resolving a capture timestamp from a raw exiftool record."""

    dt: datetime | None = None
    field: str | None = None
    """Which EXIF field supplied `dt` -- e.g. 'DateTimeOriginal' or 'Keys:CreationDate'."""
    offset_minutes: int | None = None
    """UTC offset, from OffsetTimeOriginal or one embedded in the winning field's value."""
    is_export_artifact: bool = False
    """True when a video's timestamp came from CreateDate/MediaCreateDate -- on
    Photos-exported .mov files that is the export time, not the capture time."""


def extract_timestamp(meta: dict, kind: MediaKind) -> ExifTimestamp:
    """Resolve a capture timestamp from a raw exiftool record, honoring kind-specific priority."""
    dt: datetime | None = None
    source_field: str | None = None
    for name in field_priority(kind):
        dt = parse_exif_datetime(meta.get(name))
        if dt is not None:
            source_field = name
            break

    offset = parse_offset(meta.get("OffsetTimeOriginal"))
    if offset is None and source_field is not None:
        offset = embedded_offset(meta.get(source_field))

    is_export_artifact = kind is MediaKind.VIDEO and source_field in VIDEO_EXPORT_ARTIFACT_FIELDS
    return ExifTimestamp(
        dt=dt, field=source_field, offset_minutes=offset, is_export_artifact=is_export_artifact
    )


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def run_exiftool(paths: list[Path], *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict[str, dict]:
    """Batch EXIF read, keyed by absolute source path.

    One exiftool process per chunk of `chunk_size` files, fed over stdin with `-@ -` so a huge
    folder never risks `ARG_MAX`. Deliberately never passes `-fast2` -- it skips the moov atom
    and silently zeroes video `Duration`. A chunk that fails to run (bad exiftool install,
    corrupt file wedging the process) yields no entries for that chunk rather than raising --
    callers must treat missing metadata as "nothing usable", not as a fatal error.
    """
    if not paths or not exiftool_available():
        return {}

    results: dict[str, dict] = {}
    for start in range(0, len(paths), chunk_size):
        chunk = paths[start : start + chunk_size]
        try:
            completed = subprocess.run(
                ["exiftool", "-json", "-n", *EXIFTOOL_REQUEST_FIELDS, "-@", "-"],
                input="\n".join(str(p) for p in chunk),
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if not completed.stdout.strip():
            continue
        try:
            entries = json.loads(completed.stdout)
        except json.JSONDecodeError:
            continue
        for entry in entries:
            source_file = entry.get("SourceFile")
            if source_file:
                results[source_file] = entry
    return results
