"""Cross-stage integration: the seams between stages built by separate agents.

Each stage's own tests prove it works in isolation. These prove the handoffs work, which is
where the parallel build actually went wrong: `metadata` computed the EXIF UTC offset and
discarded it, so `timezones` could never take its level-1 path and every item silently fell
through to GPS. Both stages passed their own suites throughout.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from rich.console import Console

from story_book.db import connection as db
from story_book.db.models import GpsSource, TzSource
from story_book.pipeline.base import StageContext
from story_book.pipeline.metadata import MetadataStage
from story_book.pipeline.runner import Runner
from story_book.pipeline.scan import ScanStage
from story_book.pipeline.timezones import TimezoneStage


@pytest.fixture
def scanned(ctx: StageContext, has_exiftool: bool) -> StageContext:
    if not has_exiftool:
        pytest.skip("exiftool not installed")
    ScanStage().run(ctx)
    stage = MetadataStage()
    pending = stage.select(ctx)
    for start in range(0, len(pending), stage.batch_size):
        stage.process_batch(ctx, pending[start : start + stage.batch_size])
    return ctx


def _by_name(conn: sqlite3.Connection, filename: str):
    for media in db.iter_media(conn):
        if Path(media.path).name == filename:
            return media
    raise AssertionError(f"{filename} not found in db")


class TestMetadataHandsOffTheExifOffset:
    """The regression. Before the fix these fields were left at their defaults."""

    def test_offset_is_persisted(self, scanned: StageContext) -> None:
        media = _by_name(scanned.conn, "heic_gps_offset.heic")
        assert media.tz_offset_minutes == 120

    def test_tz_source_marks_it_as_an_exif_offset(self, scanned: StageContext) -> None:
        media = _by_name(scanned.conn, "heic_gps_offset.heic")
        assert media.tz_source is TzSource.EXIF_OFFSET

    def test_a_file_without_an_offset_tag_is_left_unknown(self, scanned: StageContext) -> None:
        media = _by_name(scanned.conn, "jpeg_gps_no_offset.jpg")
        assert media.tz_source is TzSource.UNKNOWN


class TestTimezoneResolutionUsesTheHandoff:
    def test_level_one_fires_when_the_tag_agrees_with_gps(self, scanned: StageContext) -> None:
        """The path that was dead: tag present, GPS agrees, so the tag is trusted."""
        TimezoneStage().run(scanned)
        media = _by_name(scanned.conn, "heic_gps_offset.heic")
        assert media.tz_source is TzSource.EXIF_OFFSET

    def test_gps_wins_when_the_tag_disagrees(self, scanned: StageContext) -> None:
        TimezoneStage().run(scanned)
        media = _by_name(scanned.conn, "offset_gps_conflict.jpg")
        assert media.tz_source is TzSource.GPS

    def test_the_conflicting_item_is_not_left_with_its_bogus_offset(
        self, scanned: StageContext
    ) -> None:
        TimezoneStage().run(scanned)
        media = _by_name(scanned.conn, "offset_gps_conflict.jpg")
        assert media.tz_offset_minutes != -420

    def test_gps_only_items_resolve_from_coordinates(self, scanned: StageContext) -> None:
        TimezoneStage().run(scanned)
        media = _by_name(scanned.conn, "jpeg_gps_no_offset.jpg")
        assert media.tz_source is TzSource.GPS

    def test_every_dated_item_gets_a_utc_instant(self, scanned: StageContext) -> None:
        TimezoneStage().run(scanned)
        dated = [m for m in db.iter_media(scanned.conn) if m.taken_local]
        assert all(m.taken_utc for m in dated)


class TestScanMetadataSeam:
    def test_metadata_finds_every_scanned_image(self, scanned: StageContext) -> None:
        images = [m for m in db.iter_media(scanned.conn, kind="image")]
        assert all(m.width and m.height for m in images)

    def test_gps_bearing_fixtures_are_marked_as_exif_sourced(self, scanned: StageContext) -> None:
        media = _by_name(scanned.conn, "heic_gps_offset.heic")
        assert media.gps_source is GpsSource.EXIF

    def test_scan_is_still_idempotent_after_metadata_ran(self, scanned: StageContext) -> None:
        before = db.count_media(scanned.conn)
        ScanStage().run(scanned)
        assert db.count_media(scanned.conn) == before

    def test_rescanning_does_not_erase_metadata(self, scanned: StageContext) -> None:
        """`scan` upserts the same rows metadata just enriched -- it must not blank them."""
        ScanStage().run(scanned)
        media = _by_name(scanned.conn, "heic_gps_offset.heic")
        assert media.taken_local is not None


class TestAddingAFileToAnAlreadyBuiltTrip:
    """Both bugs this covers were invisible on a first run and silent on the second.

    `scan` and `timezones` are cached whole-trip stages. Without `always_run`, scan never
    re-walked the tree, and even once it did, `timezones` kept its cached result -- leaving the
    new photo with a NULL `taken_utc`, which drops it out of ordering, day grouping, and the
    timeline without any error.

    Driven through the real `Runner` rather than by calling stages directly, because the bugs
    live in the cache layer the runner owns: calling `process_batch` by hand records no
    `stage_result` rows and so never exercises the caching these tests are about.
    """

    @pytest.fixture
    def stages(self) -> list:
        return [ScanStage(), MetadataStage(), TimezoneStage()]

    @pytest.fixture
    def built(self, ctx: StageContext, stages: list, has_exiftool: bool) -> StageContext:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        Runner(ctx, stages, console=Console(quiet=True)).run()
        return ctx

    def _add_photo(self, ctx: StageContext) -> None:
        """A genuinely different file -- a byte-identical copy would collapse on its hash."""
        source = ctx.source_dir / "tz_before_1.jpg"
        (ctx.source_dir / "added_later.jpg").write_bytes(source.read_bytes()[:-1] + b"\x00")

    def _rebuild(self, ctx: StageContext, stages: list):
        return Runner(ctx, stages, console=Console(quiet=True)).run()

    def test_scan_is_marked_always_run(self) -> None:
        assert ScanStage().always_run is True

    def test_timezones_is_marked_always_run(self) -> None:
        assert TimezoneStage().always_run is True

    def test_a_rebuild_with_no_changes_recomputes_no_metadata(
        self, built: StageContext, stages: list
    ) -> None:
        report = self._rebuild(built, stages)
        metadata = next(s for s in report.stages if s.name == "metadata")
        assert metadata.done == 0

    def test_a_new_file_is_discovered_on_the_next_build(
        self, built: StageContext, stages: list
    ) -> None:
        before = db.count_media(built.conn)
        self._add_photo(built)
        self._rebuild(built, stages)
        assert db.count_media(built.conn) == before + 1

    def test_only_the_new_file_gets_reprocessed(self, built: StageContext, stages: list) -> None:
        self._add_photo(built)
        report = self._rebuild(built, stages)
        metadata = next(s for s in report.stages if s.name == "metadata")
        assert metadata.done == 1

    def test_the_new_file_gets_a_utc_instant(self, built: StageContext, stages: list) -> None:
        self._add_photo(built)
        self._rebuild(built, stages)
        assert _by_name(built.conn, "added_later.jpg").taken_utc is not None

    def test_no_dated_item_is_left_without_a_utc_instant(
        self, built: StageContext, stages: list
    ) -> None:
        self._add_photo(built)
        self._rebuild(built, stages)
        orphans = [m for m in db.iter_media(built.conn) if m.taken_local and not m.taken_utc]
        assert orphans == []

    def test_rescanning_preserves_the_resolved_timezone(
        self, built: StageContext, stages: list
    ) -> None:
        before = _by_name(built.conn, "heic_gps_offset.heic").taken_utc
        self._rebuild(built, stages)
        assert _by_name(built.conn, "heic_gps_offset.heic").taken_utc == before

    def test_the_source_tree_is_never_modified(self, built: StageContext, stages: list) -> None:
        before = {p.name: p.read_bytes() for p in built.source_dir.iterdir() if p.is_file()}
        self._rebuild(built, stages)
        after = {p.name: p.read_bytes() for p in built.source_dir.iterdir() if p.is_file()}
        assert before == after
