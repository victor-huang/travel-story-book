"""Event detection against a real temp DB."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from story_book.config import EventConfig
from story_book.db import connection as db
from story_book.db.models import MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage

VIENNA = (48.2082, 16.3738)


def _seed(
    ctx: StageContext,
    make_media,
    count: int,
    *,
    minutes: float = 5.0,
    start_hour: int = 9,
    prefix: str = "item",
):
    """Media is keyed by content hash, so a second batch needs a distinct prefix or it silently
    overwrites the first rather than adding to it."""
    start = datetime(2026, 7, 18, start_hour)
    for index in range(count):
        at = start + timedelta(minutes=minutes * index)
        db.upsert_media(
            ctx.conn,
            make_media(
                f"{prefix}{index:03d}",
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )


def _with_events(ctx: StageContext, **fields) -> StageContext:
    return replace(ctx, config=replace(ctx.config, events=EventConfig(**fields)))


def _event_rows(ctx: StageContext):
    return ctx.conn.execute("SELECT * FROM event ORDER BY day_id, seq").fetchall()


class TestEventRowsAreWritten:
    def test_events_are_created(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert len(_event_rows(ctx)) >= 1

    def test_every_dated_item_is_linked_to_an_event(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        linked = ctx.conn.execute("SELECT COUNT(*) AS n FROM media_event").fetchone()["n"]
        assert linked == 6

    def test_an_item_belongs_to_exactly_one_event(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        rows = ctx.conn.execute(
            "SELECT media_hash, COUNT(*) AS n FROM media_event GROUP BY media_hash HAVING n > 1"
        ).fetchall()
        assert rows == []

    def test_events_carry_a_time_range(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert all(row["start_utc"] and row["end_utc"] for row in _event_rows(ctx))

    def test_events_carry_a_centroid(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        row = _event_rows(ctx)[0]
        assert row["centroid_lat"] == pytest.approx(VIENNA[0], abs=0.01)

    def test_a_long_gap_creates_a_second_event(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3, minutes=5.0, start_hour=9, prefix="morning")
        _seed(ctx, make_media, 3, minutes=5.0, start_hour=15, prefix="afternoon")
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert len(_event_rows(ctx)) == 2


class TestRerunBehaviour:
    def test_a_rerun_does_not_duplicate_events(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 8)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        before = len(_event_rows(ctx))
        EventStage().run(ctx)
        assert len(_event_rows(ctx)) == before

    def test_a_rerun_does_not_duplicate_links(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 8)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        EventStage().run(ctx)
        linked = ctx.conn.execute("SELECT COUNT(*) AS n FROM media_event").fetchone()["n"]
        assert linked == 8

    def test_an_added_photo_is_placed_into_an_event(self, ctx: StageContext, make_media) -> None:
        """`always_run` earns itself here: without it a new photo belongs to no event, and
        everything downstream is scoped by event -- so it would vanish from dedup, selection,
        landmarks and the timeline at once."""
        _seed(ctx, make_media, 5)
        DaysStage().run(ctx)
        EventStage().run(ctx)

        at = datetime(2026, 7, 18, 9, 12)
        db.upsert_media(
            ctx.conn,
            make_media(
                "late_arrival",
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )
        DaysStage().run(ctx)
        EventStage().run(ctx)

        rows = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM media_event WHERE media_hash = 'late_arrival'"
        ).fetchone()
        assert rows["n"] == 1

    def test_the_stage_is_marked_always_run(self) -> None:
        assert EventStage().always_run is True


class TestUndatedAndUnplaceable:
    def test_undated_items_are_not_linked(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        db.upsert_media(ctx.conn, make_media("undated"))
        DaysStage().run(ctx)
        EventStage().run(ctx)
        rows = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM media_event WHERE media_hash = 'undated'"
        ).fetchone()
        assert rows["n"] == 0

    def test_running_without_day_rows_creates_no_events(
        self, ctx: StageContext, make_media
    ) -> None:
        """Ordering dependency stated as a test: events hang off days."""
        _seed(ctx, make_media, 4)
        EventStage().run(ctx)
        assert _event_rows(ctx) == []

    def test_an_empty_library_is_fine(self, ctx: StageContext) -> None:
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert _event_rows(ctx) == []


class TestPlaceIsAMajorityVote:
    def test_the_dominant_place_wins(self, ctx: StageContext, make_media) -> None:
        for index, place in enumerate([None, None, None]):
            at = datetime(2026, 7, 18, 9) + timedelta(minutes=index)
            db.upsert_media(
                ctx.conn,
                make_media(
                    f"p{index}",
                    taken_local=at.isoformat(),
                    taken_utc=at.isoformat(),
                    lat=VIENNA[0],
                    lon=VIENNA[1],
                    place_id=place,
                ),
            )
        cursor = ctx.conn.execute(
            "INSERT INTO place (lat_key, lon_key, city, source) VALUES (1.0, 1.0, 'A', 'offline')"
        )
        first = cursor.lastrowid
        cursor = ctx.conn.execute(
            "INSERT INTO place (lat_key, lon_key, city, source) VALUES (2.0, 2.0, 'B', 'offline')"
        )
        second = cursor.lastrowid
        for index, place in enumerate([first, second, second]):
            media = db.get_media(ctx.conn, f"p{index}")
            media.place_id = place
            db.upsert_media(ctx.conn, media)

        DaysStage().run(ctx)
        EventStage().run(ctx)

        assert _event_rows(ctx)[0]["place_id"] == second


class TestRealFixtures:
    def test_the_fixture_library_produces_events(
        self, ctx: StageContext, media_dir, has_exiftool: bool
    ) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        from story_book.pipeline.metadata import MetadataStage
        from story_book.pipeline.scan import ScanStage
        from story_book.pipeline.timezones import TimezoneStage

        ScanStage().run(ctx)
        stage = MetadataStage()
        pending = stage.select(ctx)
        for start in range(0, len(pending), stage.batch_size):
            stage.process_batch(ctx, pending[start : start + stage.batch_size])
        TimezoneStage().run(ctx)
        DaysStage().run(ctx)
        EventStage().run(ctx)

        assert len(_event_rows(ctx)) >= 1

    def test_images_and_videos_both_land_in_events(self, ctx: StageContext, make_media) -> None:
        at = datetime(2026, 7, 18, 9)
        db.upsert_media(
            ctx.conn,
            make_media(
                "photo",
                kind=MediaKind.IMAGE,
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )
        db.upsert_media(
            ctx.conn,
            make_media(
                "clip",
                kind=MediaKind.VIDEO,
                taken_local=(at + timedelta(minutes=1)).isoformat(),
                taken_utc=(at + timedelta(minutes=1)).isoformat(),
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )
        DaysStage().run(ctx)
        EventStage().run(ctx)

        linked = ctx.conn.execute("SELECT COUNT(*) AS n FROM media_event").fetchone()["n"]
        assert linked == 2
