"""Backend tests for GPS backfill: real temp DB, real fixture media.

Mirrors `tests/backend/test_timezones.py`'s approach: `Media` rows are built directly (with
`taken_utc` set explicitly, as `TimezoneStage` would leave it) rather than by running the
metadata/timezone stages, since T20 owns only `pipeline/gps_backfill.py` and must not depend on
those other tasks' modules to test its own seam. This exercises the real committed
`jpeg_no_gps.jpg` fixture (a Sony frame with no GPS) sitting between two GPS-bearing iPhone
frames -- the sparse case called out in the task brief.
"""

from __future__ import annotations

from pathlib import Path

from story_book.config import Config, TimeConfig
from story_book.db import connection as db
from story_book.db.models import GpsSource, Media, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.gps_backfill import GpsBackfillStage

SALZBURG = (47.8095, 13.0550)
VIENNA = (48.2082, 16.3738)


def _media_from_fixture(
    conn,
    path: Path,
    media_hash: str,
    *,
    taken_utc: str,
    lat: float | None = None,
    lon: float | None = None,
    gps_source: GpsSource = GpsSource.NONE,
) -> None:
    media = Media(
        hash=media_hash,
        path=str(path),
        kind=MediaKind.IMAGE,
        bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        taken_utc=taken_utc,
        lat=lat,
        lon=lon,
        gps_source=gps_source,
    )
    db.upsert_media(conn, media)


class TestSparseCaseFromRealFixtures:
    """`jpeg_no_gps.jpg` (Sony, no GPS) between two GPS-bearing iPhone frames."""

    def _load(self, ctx: StageContext, media_dir: Path) -> None:
        _media_from_fixture(
            ctx.conn,
            media_dir / "heic_gps_offset.heic",
            "before",
            taken_utc="2026-07-18T07:20:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_no_gps.jpg",
            "no_gps",
            taken_utc="2026-07-18T09:45:00+00:00",
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_gps_no_offset.jpg",
            "after",
            taken_utc="2026-07-18T09:45:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

    def test_the_gpsless_item_receives_an_interpolated_location(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        self._load(ctx, media_dir)
        GpsBackfillStage().run(ctx)

        media = db.get_media(ctx.conn, "no_gps")
        assert media.gps_source == GpsSource.INTERPOLATED
        assert media.lat is not None
        assert media.lon is not None
        assert media.gps_confidence is not None


class TestExifGpsIsNeverOverwrittenViaRealDb:
    def test_item_with_measured_gps_keeps_its_own_coordinates(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _media_from_fixture(
            ctx.conn,
            media_dir / "heic_gps_offset.heic",
            "has_gps",
            taken_utc="2026-07-18T07:20:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_gps_no_offset.jpg",
            "other",
            taken_utc="2026-07-18T09:45:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        GpsBackfillStage().run(ctx)

        media = db.get_media(ctx.conn, "has_gps")
        assert media.lat == SALZBURG[0]
        assert media.lon == SALZBURG[1]
        assert media.gps_source == GpsSource.EXIF


class TestWindowRefusalViaRealDb:
    def test_a_gap_past_the_configured_window_leaves_the_item_without_gps(
        self, conn, out_dir: Path, source_dir: Path, media_dir: Path
    ) -> None:
        config = Config(time=TimeConfig(gps_interpolation_window_minutes=30.0))
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=source_dir)
        _media_from_fixture(
            conn,
            media_dir / "heic_gps_offset.heic",
            "before",
            taken_utc="2026-07-18T07:20:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        _media_from_fixture(
            conn,
            media_dir / "jpeg_no_gps.jpg",
            "no_gps",
            taken_utc="2026-07-18T09:45:00+00:00",
        )

        GpsBackfillStage().run(ctx)

        media = db.get_media(conn, "no_gps")
        assert media.gps_source == GpsSource.NONE
        assert media.lat is None
        assert media.lon is None


class TestNoGpsBearingItemsAtAllViaRealDb:
    def test_nothing_is_filled_when_the_whole_trip_has_no_gps(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_no_gps.jpg",
            "first",
            taken_utc="2026-07-18T09:45:00+00:00",
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_no_exif.jpg",
            "second",
            taken_utc="2026-07-18T09:50:00+00:00",
        )

        GpsBackfillStage().run(ctx)

        assert db.get_media(ctx.conn, "first").gps_source == GpsSource.NONE
        assert db.get_media(ctx.conn, "second").gps_source == GpsSource.NONE


class TestIdempotentRerunViaRealDb:
    def test_running_the_stage_twice_leaves_the_result_unchanged(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _media_from_fixture(
            ctx.conn,
            media_dir / "heic_gps_offset.heic",
            "before",
            taken_utc="2026-07-18T07:20:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_no_gps.jpg",
            "no_gps",
            taken_utc="2026-07-18T09:45:00+00:00",
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_gps_no_offset.jpg",
            "after",
            taken_utc="2026-07-18T09:45:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        GpsBackfillStage().run(ctx)
        first = db.get_media(ctx.conn, "no_gps")

        GpsBackfillStage().run(ctx)
        second = db.get_media(ctx.conn, "no_gps")

        assert first == second


class TestAlwaysRunPicksUpNewlyScannedItems:
    """Regression for the exact bug the task brief calls out: without `always_run`, a cached
    result would leave an item scanned after the first run permanently GPS-less."""

    def test_a_newly_added_gpsless_item_is_filled_on_a_later_run(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _media_from_fixture(
            ctx.conn,
            media_dir / "heic_gps_offset.heic",
            "before",
            taken_utc="2026-07-18T07:20:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_gps_no_offset.jpg",
            "after",
            taken_utc="2026-07-18T09:45:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )
        GpsBackfillStage().run(ctx)

        _media_from_fixture(
            ctx.conn,
            media_dir / "jpeg_no_gps.jpg",
            "late_arrival",
            taken_utc="2026-07-18T09:45:00+00:00",
        )
        GpsBackfillStage().run(ctx)

        media = db.get_media(ctx.conn, "late_arrival")
        assert media.gps_source == GpsSource.INTERPOLATED
        assert media.lat is not None
        assert GpsBackfillStage.always_run is True
