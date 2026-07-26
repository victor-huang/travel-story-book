"""Backend tests for timezone resolution: real temp DB, real fixture media.

T11 (`pipeline/metadata.py`) is being written in parallel and, as currently checked in, never
persists the raw `OffsetTimeOriginal` tag anywhere the DB exposes it (see the module docstring
in `story_book.pipeline.timezones` for why). Per this task's brief, tests here must not import
`pipeline/metadata.py` or `story_book.exif` either, so the fixtures that carry a real EXIF offset
tag are read directly with `piexif` -- the same library the fixture generator itself uses -- to
build the `Media` rows this stage is meant to receive once T11 sets `tz_offset_minutes` /
`tz_source=EXIF_OFFSET` for a present `OffsetTimeOriginal` tag.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import piexif

from story_book.db import connection as db
from story_book.db.models import GpsSource, MediaKind, TzSource
from story_book.pipeline.base import StageContext
from story_book.pipeline.timezones import TimezoneStage


def _decimal_from_dms(dms, ref: bytes) -> float:
    degrees = dms[0][0] / dms[0][1]
    minutes = dms[1][0] / dms[1][1]
    seconds = dms[2][0] / dms[2][1]
    value = degrees + minutes / 60 + seconds / 3600
    return -value if ref in (b"S", b"W") else value


def _read_fixture_exif(path: Path) -> dict:
    """Recreate the slice of EXIF a well-behaved T11 would hand this stage."""
    data = piexif.load(str(path))
    exif_ifd = data["Exif"]
    gps_ifd = data["GPS"]

    taken_local = None
    raw_taken = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
    if raw_taken:
        taken_local = datetime.strptime(raw_taken.decode(), "%Y:%m:%d %H:%M:%S").isoformat()

    offset_minutes = None
    raw_offset = exif_ifd.get(piexif.ExifIFD.OffsetTimeOriginal)
    if raw_offset:
        text = raw_offset.decode()
        sign = -1 if text[0] == "-" else 1
        hours, minutes = int(text[1:3]), int(text[4:6])
        offset_minutes = sign * (hours * 60 + minutes)

    lat = lon = None
    if gps_ifd:
        lat = _decimal_from_dms(
            gps_ifd[piexif.GPSIFD.GPSLatitude], gps_ifd[piexif.GPSIFD.GPSLatitudeRef]
        )
        lon = _decimal_from_dms(
            gps_ifd[piexif.GPSIFD.GPSLongitude], gps_ifd[piexif.GPSIFD.GPSLongitudeRef]
        )

    return {"taken_local": taken_local, "offset_minutes": offset_minutes, "lat": lat, "lon": lon}


def _insert_device(conn: sqlite3.Connection, device_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO device (id) VALUES (?)", (device_id,))


def _media_from_fixture(conn: sqlite3.Connection, path: Path, media_hash: str, device_id: str):
    from story_book.db.models import Media

    parsed = _read_fixture_exif(path)
    _insert_device(conn, device_id)
    media = Media(
        hash=media_hash,
        path=str(path),
        kind=MediaKind.IMAGE,
        bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        device_id=device_id,
        taken_local=parsed["taken_local"],
        lat=parsed["lat"],
        lon=parsed["lon"],
        gps_source=GpsSource.EXIF if parsed["lat"] is not None else GpsSource.NONE,
        tz_offset_minutes=parsed["offset_minutes"],
        tz_source=TzSource.EXIF_OFFSET
        if parsed["offset_minutes"] is not None
        else TzSource.UNKNOWN,
    )
    db.upsert_media(conn, media)


class TestSustainedTimezoneCrossingFromRealFixtures:
    """The acceptance criterion: a trip crossing a timezone boundary lands every item on the
    correct local calendar day, using the real committed +02:00/+03:00 fixture files."""

    def _load(self, ctx: StageContext, media_dir: Path) -> None:
        for i in range(1, 4):
            _media_from_fixture(
                ctx.conn, media_dir / f"tz_before_{i}.jpg", f"before{i}", "Apple iPhone 16 Pro"
            )
        for i in range(1, 4):
            _media_from_fixture(
                ctx.conn, media_dir / f"tz_after_{i}.jpg", f"after{i}", "Apple iPhone 16 Pro"
            )

    def test_before_items_resolve_to_vienna_and_stay_on_the_19th(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        self._load(ctx, media_dir)
        TimezoneStage().run(ctx)

        for i in range(1, 4):
            media = db.get_media(ctx.conn, f"before{i}")
            assert media.tz_name == "Europe/Vienna"
            assert media.tz_offset_minutes == 120
            assert media.taken_local.startswith("2026-07-19")

    def test_after_items_resolve_to_istanbul_and_land_on_the_20th(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        self._load(ctx, media_dir)
        TimezoneStage().run(ctx)

        for i in range(1, 4):
            media = db.get_media(ctx.conn, f"after{i}")
            assert media.tz_name == "Europe/Istanbul"
            assert media.tz_offset_minutes == 180
            assert media.taken_local.startswith("2026-07-20")

    def test_exif_offset_is_trusted_because_it_agrees_with_its_own_gps(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        self._load(ctx, media_dir)
        TimezoneStage().run(ctx)

        for i in range(1, 4):
            assert db.get_media(ctx.conn, f"before{i}").tz_source == TzSource.EXIF_OFFSET
            assert db.get_media(ctx.conn, f"after{i}").tz_source == TzSource.EXIF_OFFSET


class TestOffsetGpsConflictRegressionFromRealFixture:
    """`offset_gps_conflict.jpg`: Vienna coordinates tagged with an offset 9 hours off. GPS
    must win, not the tag -- the whole point of the revised resolution order."""

    def test_gps_wins_and_the_item_lands_on_the_correct_day(
        self, ctx: StageContext, media_dir: Path
    ) -> None:
        _media_from_fixture(
            ctx.conn, media_dir / "offset_gps_conflict.jpg", "conflict", "Apple iPhone 16 Pro"
        )
        before = db.get_media(ctx.conn, "conflict")
        assert before.tz_offset_minutes == -420, "fixture precondition: tag disagrees with GPS"

        TimezoneStage().run(ctx)

        media = db.get_media(ctx.conn, "conflict")
        assert media.tz_source == TzSource.GPS
        assert media.tz_name == "Europe/Vienna"
        assert media.tz_offset_minutes == 120
        assert media.taken_local == "2026-07-19T06:15:00"
        assert media.taken_utc == "2026-07-19T04:15:00+00:00"


class TestDeviceNeighborFallbackViaRealDb:
    def test_gpsless_item_borrows_the_zone_of_a_same_device_gps_anchor(
        self, ctx: StageContext, make_media
    ) -> None:
        _insert_device(ctx.conn, "Sony ILCE-7M4")
        db.upsert_media(
            ctx.conn,
            make_media(
                "anchor",
                device_id="Sony ILCE-7M4",
                taken_local="2026-07-18T12:00:00",
                lat=47.8095,
                lon=13.0550,
                gps_source=GpsSource.EXIF,
            ),
        )
        db.upsert_media(
            ctx.conn,
            make_media("orphan", device_id="Sony ILCE-7M4", taken_local="2026-07-18T12:05:00"),
        )

        TimezoneStage().run(ctx)

        orphan = db.get_media(ctx.conn, "orphan")
        assert orphan.tz_source == TzSource.DEVICE_NEIGHBOR
        assert orphan.tz_name == "Europe/Vienna"
        assert orphan.tz_offset_minutes == 120


class TestConfigFallbackViaRealDb:
    def test_item_with_neither_offset_nor_gps_nor_device_neighbor_uses_config_default(
        self, conn: sqlite3.Connection, out_dir: Path, source_dir: Path, make_media
    ) -> None:
        from story_book.config import Config, TimeConfig

        config = Config(time=TimeConfig(default_timezone="America/New_York"))
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=source_dir)
        db.upsert_media(conn, make_media("lonely", taken_local="2026-07-18T09:00:00"))

        TimezoneStage().run(ctx)

        media = db.get_media(conn, "lonely")
        assert media.tz_source == TzSource.CONFIG
        assert media.tz_name == "America/New_York"
        assert media.tz_offset_minutes == -240  # EDT in July


class TestDeviceClockOffsetCorrectionViaRealDb:
    def test_configured_clock_offset_shifts_the_resolved_utc_instant(
        self, conn: sqlite3.Connection, out_dir: Path, source_dir: Path, make_media
    ) -> None:
        from story_book.config import Config, DeviceConfig, replace_devices

        config = replace_devices(Config(), {"Sony": DeviceConfig(clock_offset_minutes=-30)})
        ctx = StageContext(conn=conn, config=config, out_dir=out_dir, source_dir=source_dir)
        _insert_device(conn, "Sony")
        db.upsert_media(
            conn,
            make_media(
                "drifted",
                device_id="Sony",
                taken_local="2026-07-18T12:00:00",
                lat=47.8095,
                lon=13.0550,
                gps_source=GpsSource.EXIF,
            ),
        )

        TimezoneStage().run(ctx)

        media = db.get_media(conn, "drifted")
        assert media.taken_local == "2026-07-18T11:30:00"
        assert media.taken_utc == "2026-07-18T09:30:00+00:00"


class TestNoUsableTimestampViaRealDb:
    def test_media_with_no_timestamp_is_skipped_without_error(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("no_timestamp"))

        TimezoneStage().run(ctx)

        media = db.get_media(ctx.conn, "no_timestamp")
        assert media.tz_source == TzSource.UNKNOWN
        assert media.taken_utc is None


class TestIdempotentRerun:
    def test_rerunning_the_stage_does_not_change_already_resolved_fields(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(
            ctx.conn,
            make_media(
                "stable",
                taken_local="2026-07-18T09:00:00",
                lat=47.8095,
                lon=13.0550,
                gps_source=GpsSource.EXIF,
            ),
        )

        TimezoneStage().run(ctx)
        first = db.get_media(ctx.conn, "stable")

        TimezoneStage().run(ctx)
        second = db.get_media(ctx.conn, "stable")

        assert first == second


class TestCrossDeviceOrderingAcceptance:
    """ "cross-device ordering matches reality" -- `iter_media` walks in UTC order, so two
    devices in different zones must interleave by real elapsed time, not local clock time."""

    def test_utc_ordering_reflects_true_capture_order_across_two_zones(
        self, ctx: StageContext, make_media
    ) -> None:
        _insert_device(ctx.conn, "iPhone")
        _insert_device(ctx.conn, "Sony")
        # iPhone in Vienna (+02:00) shoots at 10:00 local = 08:00 UTC.
        db.upsert_media(
            ctx.conn,
            make_media(
                "vienna",
                device_id="iPhone",
                taken_local="2026-07-18T10:00:00",
                lat=47.8095,
                lon=13.0550,
                gps_source=GpsSource.EXIF,
            ),
        )
        # Sony in Istanbul (+03:00) shoots at 10:30 local = 07:30 UTC -- earlier in real time
        # despite a later-looking local clock reading.
        db.upsert_media(
            ctx.conn,
            make_media(
                "istanbul",
                device_id="Sony",
                taken_local="2026-07-18T10:30:00",
                lat=41.0082,
                lon=28.9784,
                gps_source=GpsSource.EXIF,
            ),
        )

        TimezoneStage().run(ctx)

        assert [m.hash for m in db.iter_media(ctx.conn)] == ["istanbul", "vienna"]
