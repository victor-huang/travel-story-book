"""The rendered report, against a real DB and real fixture media.

The test that matters most here is `TestEveryReferencedFileExists`. The first render produced
perfectly valid HTML in which *every image was broken*: derived images live beside the report
directory and the pages link out of it, and nothing in the markup could tell you that. Only
loading the page did.
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.export.report import REPORT_DIRNAME, render_report
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.home_filter import HomeFilterStage
from story_book.pipeline.selection import SelectionStage
from story_book.pipeline.thumbnails import ThumbnailStage
from story_book.pipeline.timeline import build_timeline

VIENNA = (48.2082, 16.3738)

SRC_PATTERN = re.compile(r'(?:src|href)="([^"#]+)"')


@pytest.fixture
def seeded(ctx: StageContext, make_media, media_dir: Path) -> StageContext:
    """A handful of real fixture photos, walked into a route, scored and selected."""
    sources = sorted(p for p in media_dir.glob("*.jpg"))
    assert sources, "fixture media missing -- run tests/fixtures/generate.py"

    start = datetime(2026, 7, 18, 9)
    for index, source in enumerate(sources[:6]):
        at = start + timedelta(minutes=7 * index)
        media_hash = f"{index:064x}"
        db.upsert_media(
            ctx.conn,
            make_media(
                media_hash,
                path=str(source),
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0] + 0.0006 * index,
                lon=VIENNA[1] + 0.0004 * index,
                width=800,
                height=600,
                tz_name="Europe/Vienna",
                gps_source="interpolated" if index == 1 else "exif",
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, overall, "
            "content_class) VALUES (?, 0.8, 0.7, 0.5, ?, 'landscape')",
            (media_hash, 0.9 - index * 0.02),
        )
    ctx.conn.commit()

    DaysStage().run(ctx)
    EventStage().run(ctx)
    HomeFilterStage().run(ctx)
    SelectionStage().run(ctx)
    stage = ThumbnailStage()
    for media in stage.select(ctx):
        stage.persist(ctx, media, stage.compute(media, ctx.config))
    return ctx


def _document(ctx: StageContext) -> dict:
    return build_timeline(ctx.conn, ctx.config, None, ctx.out_dir)


class TestRenderedFiles:
    def test_an_index_is_written(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert rendered.index.exists()

    def test_one_page_per_day(self, seeded: StageContext) -> None:
        document = _document(seeded)
        rendered = render_report(document, seeded.out_dir)
        assert len(rendered.day_pages) == len(document["days"])

    def test_the_stylesheet_is_copied_in(self, seeded: StageContext) -> None:
        render_report(_document(seeded), seeded.out_dir)
        assert (seeded.out_dir / REPORT_DIRNAME / "style.css").exists()

    def test_a_stale_page_from_a_previous_run_is_removed(self, seeded: StageContext) -> None:
        """A day page that outlives its day would be a correction that silently failed."""
        root = seeded.out_dir / REPORT_DIRNAME
        (root / "days").mkdir(parents=True, exist_ok=True)
        stale = root / "days" / "1999-01-01.html"
        stale.write_text("old")

        render_report(_document(seeded), seeded.out_dir)
        assert not stale.exists()

    def test_it_survives_a_trip_with_no_media(self, ctx: StageContext) -> None:
        rendered = render_report(build_timeline(ctx.conn, ctx.config), ctx.out_dir)
        assert rendered.index.exists()


class TestEveryReferencedFileExists:
    """Valid HTML full of broken images is the failure this catches."""

    def _local_refs(self, page: Path) -> list[str]:
        return [
            ref
            for ref in SRC_PATTERN.findall(page.read_text())
            if not ref.startswith(("http://", "https://", "mailto:"))
        ]

    def test_the_index_references_only_files_that_exist(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        missing = [
            ref
            for ref in self._local_refs(rendered.index)
            if not (rendered.index.parent / ref).resolve().exists()
        ]
        assert missing == []

    def test_every_day_page_references_only_files_that_exist(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        missing = [
            (page.name, ref)
            for page in rendered.day_pages
            for ref in self._local_refs(page)
            if not (page.parent / ref).resolve().exists()
        ]
        assert missing == []

    def test_at_least_one_thumbnail_is_actually_referenced(self, seeded: StageContext) -> None:
        """Guards the test above from passing because nothing is referenced at all."""
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert any("thumbs/" in ref for ref in self._local_refs(rendered.day_pages[0]))


class TestWorksOffline:
    def test_the_only_external_links_are_the_optional_map_links(self, seeded: StageContext) -> None:
        """Criterion 8: browsable offline. Nothing may be *fetched* from the network."""
        rendered = render_report(_document(seeded), seeded.out_dir)
        external = {
            ref
            for page in (rendered.index, *rendered.day_pages)
            for ref in SRC_PATTERN.findall(page.read_text())
            if ref.startswith(("http://", "https://"))
        }
        assert all(ref.startswith("https://www.openstreetmap.org/") for ref in external), external

    def test_no_script_tag_is_emitted(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "<script" not in rendered.index.read_text().lower()


class TestContent:
    def test_an_interpolated_fix_is_marked_on_the_day_page(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "dot interpolated" in rendered.day_pages[0].read_text()

    def test_an_unconfigured_home_is_stated_on_the_index(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "privacy filter did not run" in rendered.index.read_text()

    def test_absent_trip_context_is_stated_on_the_index(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "No trip context was supplied" in rendered.index.read_text()

    def test_a_day_page_names_every_asset_of_that_day(self, seeded: StageContext) -> None:
        document = _document(seeded)
        rendered = render_report(document, seeded.out_dir)
        text = rendered.day_pages[0].read_text()
        expected = {a["asset_id"] for a in document["assets"].values() if a["day"]}
        assert all(asset_id in text for asset_id in expected)

    def test_a_missing_thumbnail_renders_a_placeholder_not_a_broken_image(
        self, seeded: StageContext
    ) -> None:
        shutil.rmtree(seeded.out_dir / "thumbs")
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "no preview" in rendered.day_pages[0].read_text()


class TestSpeed:
    def test_a_rerender_is_well_under_the_ten_second_budget(self, seeded: StageContext) -> None:
        """Criterion 8. Generous margin on a 6-item fixture; the real 286-item trip takes ~0.1s."""
        document = _document(seeded)
        started = time.monotonic()
        render_report(document, seeded.out_dir)
        assert time.monotonic() - started < 10.0
