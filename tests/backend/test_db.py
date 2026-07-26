from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.db.models import GpsSource, MediaKind, StageStatus, TzSource


class TestConnect:
    def test_creates_database_file(self, out_dir: Path) -> None:
        db.connect(out_dir / "story.db")
        assert (out_dir / "story.db").exists()

    def test_refuses_to_create_when_create_is_false(self, out_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            db.connect(out_dir / "absent.db", create=False)

    def test_records_schema_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        assert int(row["value"]) == db.SCHEMA_VERSION

    def test_rejects_incompatible_schema_version(self, out_dir: Path) -> None:
        path = out_dir / "story.db"
        connection = db.connect(path)
        connection.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        connection.close()
        with pytest.raises(db.SchemaVersionError):
            db.connect(path)

    def test_reopening_is_idempotent(self, out_dir: Path) -> None:
        path = out_dir / "story.db"
        db.connect(path).close()
        assert db.connect(path) is not None

    def test_foreign_keys_are_enforced(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO media_event (media_hash, event_id) VALUES ('nope', 1)")


class TestTripIsSingleRow:
    def test_ensure_trip_creates_one_row(self, conn: sqlite3.Connection) -> None:
        assert conn.execute("SELECT COUNT(*) AS n FROM trip").fetchone()["n"] == 1

    def test_ensure_trip_updates_rather_than_duplicates(self, conn: sqlite3.Connection) -> None:
        db.ensure_trip(conn, "Renamed")
        assert conn.execute("SELECT name FROM trip").fetchone()["name"] == "Renamed"

    def test_second_trip_id_is_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO trip (id, name) VALUES (2, 'Other')")


class TestMediaRoundTrip:
    def test_inserted_media_is_readable(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc"))
        assert db.get_media(conn, "abc") is not None

    def test_all_fields_survive_the_round_trip(self, conn: sqlite3.Connection, make_media) -> None:
        original = make_media(
            "abc",
            kind=MediaKind.IMAGE,
            width=4032,
            height=3024,
            taken_local="2026-07-18T09:20:00",
            taken_utc="2026-07-18T07:20:00+00:00",
            tz_name="Europe/Vienna",
            tz_offset_minutes=120,
            tz_source=TzSource.EXIF_OFFSET,
            lat=47.8095,
            lon=13.055,
            gps_source=GpsSource.EXIF,
            gps_confidence=1.0,
            is_near_home=True,
        )
        db.upsert_media(conn, original)
        assert db.get_media(conn, "abc") == original

    def test_reimport_from_a_new_path_updates_rather_than_duplicates(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc", path="/a/one.jpg"))
        db.upsert_media(conn, make_media("abc", path="/b/two.jpg"))
        assert db.count_media(conn) == 1

    def test_reimport_keeps_the_latest_path(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc", path="/a/one.jpg"))
        db.upsert_media(conn, make_media("abc", path="/b/two.jpg"))
        assert db.get_media(conn, "abc").path == "/b/two.jpg"

    def test_missing_media_returns_none(self, conn: sqlite3.Connection) -> None:
        assert db.get_media(conn, "absent") is None

    def test_iter_media_orders_by_capture_time(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("late", taken_utc="2026-07-18T12:00:00+00:00"))
        db.upsert_media(conn, make_media("early", taken_utc="2026-07-18T09:00:00+00:00"))
        assert [m.hash for m in db.iter_media(conn)] == ["early", "late"]

    def test_undated_media_sorts_last(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("undated"))
        db.upsert_media(conn, make_media("dated", taken_utc="2026-07-18T09:00:00+00:00"))
        assert [m.hash for m in db.iter_media(conn)][-1] == "undated"

    def test_iter_media_can_filter_by_kind(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("photo", kind=MediaKind.IMAGE))
        db.upsert_media(conn, make_media("movie", kind=MediaKind.VIDEO))
        assert [m.hash for m in db.iter_media(conn, kind="video")] == ["movie"]

    def test_invalid_kind_is_rejected_by_the_schema(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO media (hash, path, kind, bytes, mtime) VALUES ('x','/x','audio',1,1)"
            )


class TestStageResultCache:
    def test_completed_hash_is_reported(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        assert db.completed_hashes(conn, "scan", 1) == {"abc"}

    def test_a_different_version_invalidates_the_cache(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        assert db.completed_hashes(conn, "scan", 2) == set()

    def test_a_different_stage_has_its_own_cache(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        assert db.completed_hashes(conn, "quality", 1) == set()

    def test_skipped_counts_as_complete(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "video", 1, StageStatus.SKIPPED, "not a video")
        assert db.completed_hashes(conn, "video", 1) == {"abc"}

    def test_failed_does_not_count_as_complete_so_it_retries(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.FAILED, "boom")
        assert db.completed_hashes(conn, "scan", 1) == set()

    def test_recording_twice_updates_rather_than_duplicates(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.FAILED, "boom")
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        assert db.get_stage_result(conn, "abc", "scan").status is StageStatus.OK

    def test_error_message_is_retained(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.FAILED, "boom")
        assert db.stage_failures(conn, "scan")[0].error == "boom"

    def test_clear_stage_removes_only_that_stage(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        db.record_stage_result(conn, "abc", "quality", 1, StageStatus.OK)
        db.clear_stage(conn, "scan")
        assert db.completed_hashes(conn, "quality", 1) == {"abc"}

    def test_clear_stage_reports_how_many_it_cleared(
        self, conn: sqlite3.Connection, make_media
    ) -> None:
        db.upsert_media(conn, make_media("abc"))
        db.record_stage_result(conn, "abc", "scan", 1, StageStatus.OK)
        assert db.clear_stage(conn, "scan") == 1

    def test_whole_trip_sentinel_is_accepted_without_a_media_row(
        self, conn: sqlite3.Connection
    ) -> None:
        db.record_stage_result(conn, "__trip__", "timeline", 1, StageStatus.OK)
        assert db.completed_hashes(conn, "timeline", 1) == {"__trip__"}


class TestCascade:
    def test_deleting_media_removes_its_score(self, conn: sqlite3.Connection, make_media) -> None:
        db.upsert_media(conn, make_media("abc"))
        conn.execute("INSERT INTO score (media_hash, overall) VALUES ('abc', 0.9)")
        conn.execute("DELETE FROM media WHERE hash = 'abc'")
        assert conn.execute("SELECT COUNT(*) AS n FROM score").fetchone()["n"] == 0
