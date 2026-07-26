"""Backend tests for `DaysStage`: real temp DB, real fixture media where relevant."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from story_book.config import Config, TimeConfig
from story_book.db import connection as db
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage


def _local_dates(conn: sqlite3.Connection) -> set[str]:
    return {row["local_date"] for row in conn.execute("SELECT local_date FROM day")}


class TestLateNightAcceptanceViaRealDb:
    """ "A late-night sequence stays with the evening it began" against a real DB, default
    `day_start_hour = 4`."""

    def test_2330_and_0130_next_day_produce_a_single_day_row(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("evening", taken_local="2026-07-19T23:30:00"))
        db.upsert_media(ctx.conn, make_media("after_midnight", taken_local="2026-07-20T01:30:00"))

        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19"}


class TestTimezoneCrossingFixtureViaRealDb:
    """The committed `tz_before_*`/`tz_after_*` fixtures straddle a +02:00/+03:00 crossing.
    Resolved by T12, `tz_before` items read as Vienna local time on the 19th and `tz_after`
    items as Istanbul local time on the 20th, despite being minutes apart in real (UTC) time --
    a genuinely tricky case for day assignment, since the naive local dates disagree even though
    the day-start-hour rule still buckets them consistently per item."""

    def _load_resolved_fixtures(self, ctx: StageContext, media_dir: Path) -> None:
        for i in range(1, 4):
            db.upsert_media(
                ctx.conn,
                self._media(
                    ctx,
                    media_dir,
                    f"before{i}",
                    "tz_before",
                    i,
                    taken_local=f"2026-07-19T23:{10 + (i - 1) * 10}:00",
                    taken_utc=f"2026-07-19T21:{10 + (i - 1) * 10}:00+00:00",
                ),
            )
        for i in range(1, 4):
            db.upsert_media(
                ctx.conn,
                self._media(
                    ctx,
                    media_dir,
                    f"after{i}",
                    "tz_after",
                    i,
                    taken_local=f"2026-07-20T00:{10 + (i - 1) * 10}:00",
                    taken_utc=f"2026-07-19T21:{10 + (i - 1) * 10}:00+00:00",
                ),
            )

    def _media(self, ctx, media_dir, media_hash, prefix, index, *, taken_local, taken_utc):
        from story_book.db.models import Media, MediaKind

        path = media_dir / f"{prefix}_{index}.jpg"
        return Media(
            hash=media_hash,
            path=str(path),
            kind=MediaKind.IMAGE,
            bytes=path.stat().st_size,
            mtime=path.stat().st_mtime,
            taken_local=taken_local,
            taken_utc=taken_utc,
        )

    def test_before_and_after_items_bucket_into_the_same_day(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        self._load_resolved_fixtures(ctx, media_dir)

        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19"}


class TestDayStartHourZeroViaRealDb:
    def test_behaves_as_plain_calendar_days(self, out_dir, source_dir, make_media) -> None:
        config = Config(time=TimeConfig(day_start_hour=0))
        conn = db.connect(out_dir / db.DB_FILENAME)
        db.ensure_trip(conn, "Test Trip")
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=source_dir)
        db.upsert_media(conn, make_media("late", taken_local="2026-07-19T23:59:00"))
        db.upsert_media(conn, make_media("early", taken_local="2026-07-20T00:01:00"))

        DaysStage().run(ctx)

        assert _local_dates(conn) == {"2026-07-19", "2026-07-20"}
        conn.close()


class TestUndatedItemsAreCountedNotDropped:
    def test_undated_item_produces_no_day_and_is_logged(
        self, ctx: StageContext, make_media, caplog
    ) -> None:
        db.upsert_media(ctx.conn, make_media("no_timestamp", taken_local=None))
        db.upsert_media(ctx.conn, make_media("dated", taken_local="2026-07-19T10:00:00"))

        with caplog.at_level(logging.WARNING):
            DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19"}
        assert any(
            "1 item" in record.message and "no usable timestamp" in record.message
            for record in caplog.records
        )


class TestSingleItemViaRealDb:
    def test_a_single_item_produces_exactly_one_day(self, ctx: StageContext, make_media) -> None:
        db.upsert_media(ctx.conn, make_media(taken_local="2026-07-19T10:00:00"))

        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19"}


class TestSuspiciousGapWarnsButNeverSplits:
    def test_a_large_gap_logs_a_warning_and_still_produces_two_days_not_a_split(
        self, ctx: StageContext, make_media, caplog
    ) -> None:
        config = Config(time=TimeConfig(suspicious_gap_days=2.0))
        ctx = StageContext(
            conn=ctx.conn, config=config, out_dir=ctx.out_dir, source_dir=ctx.source_dir
        )
        db.upsert_media(
            ctx.conn,
            make_media(
                "before", taken_local="2026-07-19T10:00:00", taken_utc="2026-07-19T10:00:00+00:00"
            ),
        )
        db.upsert_media(
            ctx.conn,
            make_media(
                "after", taken_local="2026-07-25T10:00:00", taken_utc="2026-07-25T10:00:00+00:00"
            ),
        )

        with caplog.at_level(logging.WARNING):
            DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19", "2026-07-25"}
        assert any("suspicious_gap_days" in record.message for record in caplog.records)


class TestDayRowsDoNotDuplicateAcrossRuns:
    def test_running_twice_leaves_exactly_one_row_per_date(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("one", taken_local="2026-07-19T10:00:00"))
        db.upsert_media(ctx.conn, make_media("two", taken_local="2026-07-20T10:00:00"))

        DaysStage().run(ctx)
        DaysStage().run(ctx)

        rows = ctx.conn.execute(
            "SELECT local_date, COUNT(*) AS n FROM day GROUP BY local_date"
        ).fetchall()
        assert {row["local_date"]: row["n"] for row in rows} == {
            "2026-07-19": 1,
            "2026-07-20": 1,
        }

    def test_a_second_run_with_a_newly_added_item_grows_the_day_set(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("one", taken_local="2026-07-19T10:00:00"))
        DaysStage().run(ctx)
        assert _local_dates(ctx.conn) == {"2026-07-19"}

        db.upsert_media(ctx.conn, make_media("two", taken_local="2026-07-21T10:00:00"))
        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19", "2026-07-21"}

    def test_a_date_with_no_more_media_and_no_events_is_removed_on_rerun(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("one", taken_local="2026-07-19T10:00:00"))
        DaysStage().run(ctx)
        assert _local_dates(ctx.conn) == {"2026-07-19"}

        ctx.conn.execute(
            "UPDATE media SET taken_local = ? WHERE hash = 'one'", ("2026-07-20T10:00:00",)
        )
        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-20"}

    def test_a_date_with_attached_events_is_not_removed(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("one", taken_local="2026-07-19T10:00:00"))
        DaysStage().run(ctx)
        day_row = ctx.conn.execute("SELECT id FROM day WHERE local_date = '2026-07-19'").fetchone()
        ctx.conn.execute("INSERT INTO event (day_id, seq) VALUES (?, 1)", (day_row["id"],))

        ctx.conn.execute(
            "UPDATE media SET taken_local = ? WHERE hash = 'one'", ("2026-07-20T10:00:00",)
        )
        DaysStage().run(ctx)

        assert _local_dates(ctx.conn) == {"2026-07-19", "2026-07-20"}


class TestTripDateRangeIsSet:
    def test_trip_start_and_end_local_are_derived_from_observed_range(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("first", taken_local="2026-07-19T08:00:00"))
        db.upsert_media(ctx.conn, make_media("last", taken_local="2026-07-22T20:00:00"))

        DaysStage().run(ctx)

        trip = ctx.conn.execute("SELECT start_local, end_local FROM trip WHERE id = 1").fetchone()
        assert trip["start_local"] == "2026-07-19T08:00:00"
        assert trip["end_local"] == "2026-07-22T20:00:00"

    def test_a_stale_trip_range_is_recomputed(self, ctx: StageContext, make_media) -> None:
        """Derived, not user-set: guarding it against overwrite left it permanently wrong.

        The original version of this test asserted the opposite -- that an existing range is
        preserved -- which encoded the bug. Adding a photo from an earlier date to a built trip
        left `start_local` stale forever: the stage re-ran, computed the right answer, and declined
        to store it.
        """
        ctx.conn.execute(
            "UPDATE trip SET start_local = ?, end_local = ? WHERE id = 1",
            ("2020-01-01T00:00:00", "2020-01-05T00:00:00"),
        )
        db.upsert_media(ctx.conn, make_media("item", taken_local="2026-07-19T08:00:00"))

        DaysStage().run(ctx)

        trip = ctx.conn.execute("SELECT start_local, end_local FROM trip WHERE id = 1").fetchone()
        assert trip["start_local"] == "2026-07-19T08:00:00"

    def test_adding_an_earlier_photo_extends_the_range(self, ctx: StageContext, make_media) -> None:
        db.upsert_media(ctx.conn, make_media("later", taken_local="2026-07-19T08:00:00"))
        DaysStage().run(ctx)

        db.upsert_media(ctx.conn, make_media("earlier", taken_local="2026-07-17T20:00:00"))
        DaysStage().run(ctx)

        trip = ctx.conn.execute("SELECT start_local FROM trip WHERE id = 1").fetchone()
        assert trip["start_local"] == "2026-07-17T20:00:00"
