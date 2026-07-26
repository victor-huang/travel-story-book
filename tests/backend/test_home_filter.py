"""Backend tests for the home-location privacy filter: real temp DB, real fixture media.

Uses the committed `tz_before_*.jpg` (Vienna, 48.2082/16.3738) and `tz_after_*.jpg` (Istanbul,
41.0082/28.9784) fixtures as the "near home" / "far from home" pair called out in the task brief.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import piexif
import pytest

from story_book.config import Config, HomeLocation
from story_book.db import connection as db
from story_book.db.models import GpsSource, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.home_filter import HomeFilterStage, should_exclude_from_export

VIENNA = (48.2082, 16.3738)
ISTANBUL = (41.0082, 28.9784)


def _decimal_from_dms(dms, ref: bytes) -> float:
    degrees = dms[0][0] / dms[0][1]
    minutes = dms[1][0] / dms[1][1]
    seconds = dms[2][0] / dms[2][1]
    value = degrees + minutes / 60 + seconds / 3600
    return -value if ref in (b"S", b"W") else value


def _gps_from_fixture(path: Path) -> tuple[float, float]:
    data = piexif.load(str(path))
    gps_ifd = data["GPS"]
    lat = _decimal_from_dms(
        gps_ifd[piexif.GPSIFD.GPSLatitude], gps_ifd[piexif.GPSIFD.GPSLatitudeRef]
    )
    lon = _decimal_from_dms(
        gps_ifd[piexif.GPSIFD.GPSLongitude], gps_ifd[piexif.GPSIFD.GPSLongitudeRef]
    )
    return lat, lon


def _load_fixture_media(conn: sqlite3.Connection, path: Path, media_hash: str) -> None:
    from story_book.db.models import Media

    lat, lon = _gps_from_fixture(path)
    media = Media(
        hash=media_hash,
        path=str(path),
        kind=MediaKind.IMAGE,
        bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        lat=lat,
        lon=lon,
        gps_source=GpsSource.EXIF,
    )
    db.upsert_media(conn, media)


@pytest.fixture
def home_config() -> Config:
    return Config(home=HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0))


@pytest.fixture
def home_ctx(
    conn: sqlite3.Connection, home_config: Config, out_dir: Path, source_dir: Path
) -> StageContext:
    return StageContext(conn=conn, config=home_config, out_dir=out_dir, source_dir=source_dir)


class TestAcceptanceCriterionFromRealFixtures:
    """ "A fixture near the configured home never appears in any export output." Exports (T40/
    T41) don't exist yet, so this verifies the enforceable half: the fixture is flagged, and the
    exposed predicate excludes it. End-to-end export exclusion is only verified once T40/T41
    land and call `should_exclude_from_export` themselves.
    """

    def test_vienna_fixture_at_home_is_flagged_and_excluded(
        self, home_ctx: StageContext, media_dir: Path
    ) -> None:
        _load_fixture_media(home_ctx.conn, media_dir / "tz_before_1.jpg", "vienna_home")

        HomeFilterStage().run(home_ctx)

        media = db.get_media(home_ctx.conn, "vienna_home")
        assert media.is_near_home is True
        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is True
        )

    def test_istanbul_fixture_far_from_home_is_not_flagged_or_excluded(
        self, home_ctx: StageContext, media_dir: Path
    ) -> None:
        _load_fixture_media(home_ctx.conn, media_dir / "tz_after_1.jpg", "istanbul_away")

        HomeFilterStage().run(home_ctx)

        media = db.get_media(home_ctx.conn, "istanbul_away")
        assert media.is_near_home is False
        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is False
        )


class TestNoHomeConfigured:
    def test_no_op_and_no_flags_set_without_a_configured_home(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("nearby", lat=VIENNA[0], lon=VIENNA[1]))

        HomeFilterStage().run(ctx)

        media = db.get_media(ctx.conn, "nearby")
        assert media.is_near_home is False

    def test_logs_a_clear_warning_that_it_never_checked(
        self, ctx: StageContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING", logger="story_book.pipeline.home_filter")

        HomeFilterStage().run(ctx)

        assert any("no `home` configured" in record.message for record in caplog.records)


class TestItemsWithoutCoordinates:
    def test_item_without_coordinates_is_not_flagged(
        self, home_ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(home_ctx.conn, make_media("no_gps", lat=None, lon=None))

        HomeFilterStage().run(home_ctx)

        media = db.get_media(home_ctx.conn, "no_gps")
        assert media.is_near_home is False

    def test_item_without_coordinates_is_still_excluded_from_export_by_default(
        self, home_ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(home_ctx.conn, make_media("no_gps", lat=None, lon=None))

        HomeFilterStage().run(home_ctx)

        media = db.get_media(home_ctx.conn, "no_gps")
        assert (
            should_exclude_from_export(
                media, HomeLocation(lat=48.2082, lon=16.3738, exclusion_km=5.0)
            )
            is True
        )

    def test_logs_a_warning_naming_the_untested_count(
        self, home_ctx: StageContext, make_media, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING", logger="story_book.pipeline.home_filter")
        db.upsert_media(home_ctx.conn, make_media("no_gps", lat=None, lon=None))

        HomeFilterStage().run(home_ctx)

        assert any("could not be distance-tested" in record.message for record in caplog.records)


class TestIdempotentRerun:
    def test_rerunning_leaves_the_flag_unchanged(self, home_ctx: StageContext, make_media) -> None:
        db.upsert_media(home_ctx.conn, make_media("stable", lat=VIENNA[0], lon=VIENNA[1]))

        HomeFilterStage().run(home_ctx)
        first = db.get_media(home_ctx.conn, "stable")

        HomeFilterStage().run(home_ctx)
        second = db.get_media(home_ctx.conn, "stable")

        assert first == second
        assert first.is_near_home is True


class TestStaleFlagIsCorrectedWhenHomeMoves:
    def test_previously_flagged_item_is_unflagged_once_home_config_changes(
        self,
        conn: sqlite3.Connection,
        out_dir: Path,
        source_dir: Path,
        make_media,
    ) -> None:
        vienna_config = Config(home=HomeLocation(lat=VIENNA[0], lon=VIENNA[1], exclusion_km=5.0))
        vienna_ctx = StageContext(
            conn=conn, config=vienna_config, out_dir=out_dir, source_dir=source_dir
        )
        db.upsert_media(conn, make_media("traveler", lat=VIENNA[0], lon=VIENNA[1]))
        HomeFilterStage().run(vienna_ctx)
        assert db.get_media(conn, "traveler").is_near_home is True

        istanbul_config = Config(
            home=HomeLocation(lat=ISTANBUL[0], lon=ISTANBUL[1], exclusion_km=5.0)
        )
        istanbul_ctx = StageContext(
            conn=conn, config=istanbul_config, out_dir=out_dir, source_dir=source_dir
        )
        HomeFilterStage().run(istanbul_ctx)

        assert db.get_media(conn, "traveler").is_near_home is False
