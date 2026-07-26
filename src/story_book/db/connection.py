"""Database open/create plus the stage-result cache that makes runs resumable."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from story_book.db.models import Media, StageResult, StageStatus

SCHEMA_VERSION = 1
DB_FILENAME = "story.db"


class SchemaVersionError(Exception):
    """Raised when an existing database was written by an incompatible version."""


def schema_sql() -> str:
    return resources.files("story_book.db").joinpath("schema.sql").read_text()


def connect(db_path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the trip database, creating and migrating-checking it as needed."""
    if not db_path.exists() and not create:
        raise FileNotFoundError(f"no database at {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    conn.executescript(schema_sql())
    _check_schema_version(conn)
    return conn


def _check_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        return
    found = int(row["value"])
    if found != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema version {found} != {SCHEMA_VERSION}. "
            "Delete the output directory and rebuild -- originals are never touched."
        )


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- media ----------------------------------------------------------------------------


def upsert_media(conn: sqlite3.Connection, media: Media) -> None:
    """Insert or update a media row. Keyed on content hash, so re-import is a no-op."""
    conn.execute(
        """
        INSERT INTO media (
            hash, path, kind, bytes, mtime, width, height, duration, device_id,
            taken_local, taken_utc, tz_name, tz_offset_minutes, tz_source,
            lat, lon, altitude, gps_source, gps_confidence, place_id, is_near_home
        ) VALUES (
            :hash, :path, :kind, :bytes, :mtime, :width, :height, :duration, :device_id,
            :taken_local, :taken_utc, :tz_name, :tz_offset_minutes, :tz_source,
            :lat, :lon, :altitude, :gps_source, :gps_confidence, :place_id, :is_near_home
        )
        ON CONFLICT (hash) DO UPDATE SET
            path = excluded.path,
            bytes = excluded.bytes,
            mtime = excluded.mtime,
            width = excluded.width,
            height = excluded.height,
            duration = excluded.duration,
            device_id = excluded.device_id,
            taken_local = excluded.taken_local,
            taken_utc = excluded.taken_utc,
            tz_name = excluded.tz_name,
            tz_offset_minutes = excluded.tz_offset_minutes,
            tz_source = excluded.tz_source,
            lat = excluded.lat,
            lon = excluded.lon,
            altitude = excluded.altitude,
            gps_source = excluded.gps_source,
            gps_confidence = excluded.gps_confidence,
            place_id = excluded.place_id,
            is_near_home = excluded.is_near_home
        """,
        {
            "hash": media.hash,
            "path": media.path,
            "kind": str(media.kind),
            "bytes": media.bytes,
            "mtime": media.mtime,
            "width": media.width,
            "height": media.height,
            "duration": media.duration,
            "device_id": media.device_id,
            "taken_local": media.taken_local,
            "taken_utc": media.taken_utc,
            "tz_name": media.tz_name,
            "tz_offset_minutes": media.tz_offset_minutes,
            "tz_source": str(media.tz_source),
            "lat": media.lat,
            "lon": media.lon,
            "altitude": media.altitude,
            "gps_source": str(media.gps_source),
            "gps_confidence": media.gps_confidence,
            "place_id": media.place_id,
            "is_near_home": int(media.is_near_home),
        },
    )


def get_media(conn: sqlite3.Connection, media_hash: str) -> Media | None:
    row = conn.execute("SELECT * FROM media WHERE hash = ?", (media_hash,)).fetchone()
    return Media.from_row(row) if row else None


def iter_media(conn: sqlite3.Connection, *, kind: str | None = None) -> Iterator[Media]:
    """Walk media in capture order. Ordered by UTC so cross-device sequence is correct."""
    sql = "SELECT * FROM media"
    params: tuple[str, ...] = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY taken_utc IS NULL, taken_utc, hash"
    for row in conn.execute(sql, params):
        yield Media.from_row(row)


def count_media(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM media").fetchone()["n"])


# --- stage results --------------------------------------------------------------------


def record_stage_result(
    conn: sqlite3.Connection,
    media_hash: str,
    stage: str,
    stage_version: int,
    status: StageStatus,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO stage_result (media_hash, stage, stage_version, status, error, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (media_hash, stage) DO UPDATE SET
            stage_version = excluded.stage_version,
            status = excluded.status,
            error = excluded.error,
            computed_at = excluded.computed_at
        """,
        (media_hash, stage, stage_version, str(status), error, now_iso()),
    )


def get_stage_result(conn: sqlite3.Connection, media_hash: str, stage: str) -> StageResult | None:
    row = conn.execute(
        "SELECT * FROM stage_result WHERE media_hash = ? AND stage = ?",
        (media_hash, stage),
    ).fetchone()
    if row is None:
        return None
    return StageResult(
        media_hash=row["media_hash"],
        stage=row["stage"],
        stage_version=row["stage_version"],
        status=StageStatus(row["status"]),
        computed_at=row["computed_at"],
        error=row["error"],
    )


def completed_hashes(conn: sqlite3.Connection, stage: str, stage_version: int) -> set[str]:
    """Hashes already done for this stage at this version -- the resume set.

    A 'failed' result is not complete: a retry is cheap and the failure may have been
    transient (network, closed laptop). A 'skipped' result is complete by definition.
    """
    rows = conn.execute(
        """
        SELECT media_hash FROM stage_result
        WHERE stage = ? AND stage_version = ? AND status IN ('ok', 'skipped')
        """,
        (stage, stage_version),
    )
    return {row["media_hash"] for row in rows}


def clear_stage(conn: sqlite3.Connection, stage: str) -> int:
    """Drop cached results for one stage so it recomputes. Used by --force."""
    cursor = conn.execute("DELETE FROM stage_result WHERE stage = ?", (stage,))
    return cursor.rowcount


def stage_failures(conn: sqlite3.Connection, stage: str) -> list[StageResult]:
    rows = conn.execute(
        "SELECT * FROM stage_result WHERE stage = ? AND status = 'failed'", (stage,)
    )
    return [
        StageResult(
            media_hash=row["media_hash"],
            stage=row["stage"],
            stage_version=row["stage_version"],
            status=StageStatus(row["status"]),
            computed_at=row["computed_at"],
            error=row["error"],
        )
        for row in rows
    ]


# --- trip -----------------------------------------------------------------------------


def ensure_trip(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        """
        INSERT INTO trip (id, name) VALUES (1, ?)
        ON CONFLICT (id) DO UPDATE SET name = excluded.name
        """,
        (name,),
    )
