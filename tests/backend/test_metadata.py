"""Backend tests for the metadata stage: real temp DB, real committed fixture media.

Exiftool must actually run for these -- mocked-only coverage of EXIF extraction is exactly how
these bugs escape (per `CLAUDE.md`). Skipped when `exiftool` is not installed, since these tests
truly invoke the binary rather than relying on it as a skip proxy.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.db.models import GpsSource, Media, MediaKind
from story_book.exif import extract_timestamp, run_exiftool
from story_book.pipeline.base import StageContext
from story_book.pipeline.metadata import MetadataStage


def _seed(ctx: StageContext, filename: str, kind: MediaKind = MediaKind.IMAGE) -> Media:
    path = ctx.source_dir / filename
    media = Media(hash=filename, path=str(path), kind=kind, bytes=path.stat().st_size, mtime=0.0)
    db.upsert_media(ctx.conn, media)
    return media


@pytest.fixture(autouse=True)
def _require_exiftool(has_exiftool: bool) -> None:
    if not has_exiftool:
        pytest.skip("exiftool not installed")


class TestAvailable:
    def test_available_when_exiftool_installed(self, ctx: StageContext) -> None:
        available, reason = MetadataStage().available(ctx)
        assert available is True
        assert reason == ""


class TestHeicGpsOffsetFixture:
    def test_extracts_timestamp_dimensions_gps_and_device(self, ctx: StageContext) -> None:
        media = _seed(ctx, "heic_gps_offset.heic")

        MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert stored.taken_local == "2026-07-18T09:20:00"
        assert stored.device_id == "Apple iPhone 16 Pro"
        assert stored.gps_source == GpsSource.EXIF
        assert stored.lat == pytest.approx(47.8095, abs=1e-3)
        assert stored.lon == pytest.approx(13.0550, abs=1e-3)
        assert stored.width and stored.height

    def test_upserts_a_device_row(self, ctx: StageContext) -> None:
        media = _seed(ctx, "heic_gps_offset.heic")

        MetadataStage().process_batch(ctx, [media])

        row = ctx.conn.execute(
            "SELECT make, model FROM device WHERE id = 'Apple iPhone 16 Pro'"
        ).fetchone()
        assert row["make"] == "Apple"
        assert row["model"] == "iPhone 16 Pro"


class TestJpegNoGpsFixture:
    def test_gps_source_is_none_without_coordinates(self, ctx: StageContext) -> None:
        media = _seed(ctx, "jpeg_no_gps.jpg")

        MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert stored.lat is None
        assert stored.lon is None
        assert stored.gps_source == GpsSource.NONE
        assert stored.device_id == "Sony ILCE-7M4"


class TestJpegNoExifFixture:
    def test_no_exif_fixture_produces_nulls_without_error(self, ctx: StageContext) -> None:
        media = _seed(ctx, "jpeg_no_exif.jpg")

        results = MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert results == {media.hash: True}
        assert stored.taken_local is None
        assert stored.device_id is None
        assert stored.lat is None
        assert stored.gps_source == GpsSource.NONE

    def test_leaves_timezone_fields_untouched(self, ctx: StageContext) -> None:
        media = _seed(ctx, "jpeg_no_exif.jpg")

        MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert stored.taken_utc is None
        assert stored.tz_name is None


class TestClipSpeechVideoFixture:
    def test_duration_is_not_zeroed_by_fast2(self, ctx: StageContext) -> None:
        """Regression for the binding P01 finding: -fast2 skips the moov atom and silently
        zeroes video Duration with no error."""
        path = ctx.source_dir / "clip_speech.mov"
        if not path.exists():
            pytest.skip("video fixture missing -- requires ffmpeg to generate")
        media = _seed(ctx, "clip_speech.mov", kind=MediaKind.VIDEO)

        MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert stored.duration == pytest.approx(3.0, abs=0.5)

    def test_video_with_zeroed_placeholder_dates_yields_no_fabricated_timestamp(
        self, ctx: StageContext
    ) -> None:
        """This synthetic clip carries no Keys:CreationDate and only a zeroed placeholder
        CreateDate ('0000:00:00 00:00:00') -- exiftool's marker for "not actually set". The
        stage must not mistake that for a real timestamp; it should degrade to null rather
        than fabricate a date."""
        path = ctx.source_dir / "clip_speech.mov"
        if not path.exists():
            pytest.skip("video fixture missing -- requires ffmpeg to generate")
        media = _seed(ctx, "clip_speech.mov", kind=MediaKind.VIDEO)

        MetadataStage().process_batch(ctx, [media])
        stored = db.get_media(ctx.conn, media.hash)

        assert stored.taken_local is None


class TestAllFixturesAcceptanceCriterion:
    """'All fixture files yield correct metadata; the no-EXIF fixture produces nulls without
    error.'"""

    IMAGE_FIXTURES = (
        "heic_gps_offset.heic",
        "jpeg_gps_no_offset.jpg",
        "jpeg_no_gps.jpg",
        "jpeg_no_exif.jpg",
        "burst_a.jpg",
        "burst_b.jpg",
        "exact_a.jpg",
        "exact_b.jpg",
        "distinct_a.jpg",
        "distinct_b.jpg",
        "sharp.jpg",
        "blurred.jpg",
        "screenshot.jpg",
        "receipt.jpg",
        "overexposed.jpg",
        "underexposed.jpg",
        "offset_gps_conflict.jpg",
        "tz_before_1.jpg",
        "tz_before_2.jpg",
        "tz_before_3.jpg",
        "tz_after_1.jpg",
        "tz_after_2.jpg",
        "tz_after_3.jpg",
    )

    def test_every_image_fixture_processes_without_raising(self, ctx: StageContext) -> None:
        stage = MetadataStage()
        batch = [
            _seed(ctx, name, kind=MediaKind.IMAGE)
            for name in self.IMAGE_FIXTURES
            if (ctx.source_dir / name).exists()
        ]

        results = stage.process_batch(ctx, batch)

        assert set(results) == {media.hash for media in batch}
        for media in batch:
            stored = db.get_media(ctx.conn, media.hash)
            assert stored.width and stored.height

    def test_video_fixtures_process_without_raising(self, ctx: StageContext) -> None:
        stage = MetadataStage()
        names = ["clip_speech.mov", "clip_silent.mp4"]
        batch = [
            _seed(ctx, name, kind=MediaKind.VIDEO)
            for name in names
            if (ctx.source_dir / name).exists()
        ]
        if not batch:
            pytest.skip("video fixtures missing -- requires ffmpeg to generate")

        results = stage.process_batch(ctx, batch)

        assert set(results) == {media.hash for media in batch}
        for media in batch:
            stored = db.get_media(ctx.conn, media.hash)
            assert stored.duration is not None and stored.duration > 0


class TestPhotosExportedVideoUsesCaptureTimeNotExportTime:
    """End-to-end regression for the P01 finding, against a real file.

    `clip_apple_export.mov` carries `Keys:CreationDate` = capture time and a `QuickTime:CreateDate`
    eight days later standing in for the export time -- the exact shape of a Photos-exported clip.
    Until this fixture existed the rule was covered only by mocked dicts, because the other video
    fixtures have a `0000:00:00` placeholder and no `Keys:CreationDate` to prefer.
    """

    def test_the_capture_time_wins(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_apple_export.mov"
        meta = run_exiftool([target])[str(target)]
        timestamp = extract_timestamp(meta, MediaKind.VIDEO)
        assert timestamp.dt == datetime(2026, 7, 18, 11, 37, 58)

    def test_the_export_date_is_not_used(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_apple_export.mov"
        timestamp = extract_timestamp(run_exiftool([target])[str(target)], MediaKind.VIDEO)
        assert timestamp.dt.date() != date(2026, 7, 26)

    def test_the_source_field_is_recorded_as_creation_date(
        self, media_dir: Path, has_exiftool: bool
    ) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_apple_export.mov"
        timestamp = extract_timestamp(run_exiftool([target])[str(target)], MediaKind.VIDEO)
        assert timestamp.field == "CreationDate"

    def test_it_is_not_flagged_as_an_export_artifact(
        self, media_dir: Path, has_exiftool: bool
    ) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_apple_export.mov"
        timestamp = extract_timestamp(run_exiftool([target])[str(target)], MediaKind.VIDEO)
        assert timestamp.is_export_artifact is False

    def test_the_embedded_offset_is_recovered(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_apple_export.mov"
        timestamp = extract_timestamp(run_exiftool([target])[str(target)], MediaKind.VIDEO)
        assert timestamp.offset_minutes == 120
