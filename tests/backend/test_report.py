"""The rendered report, against a real DB and real fixture media.

The test that matters most here is `TestEveryReferencedFileExists`. The first render produced
perfectly valid HTML in which *every image was broken*: derived images live beside the report
directory and the pages link out of it, and nothing in the markup could tell you that. Only
loading the page did.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.export.report import REPORT_DIRNAME, MediaPrefix, render_report
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
    """Criterion 8, restated after the map became Leaflet.

    Tiles are now fetched, deliberately. What must not happen is *code* coming from a network:
    a CDN reference makes the report depend on someone else's uptime and leaks a request every
    time it is opened. The previous version of these two tests kept passing after the change and
    said nothing true -- one only looked at the index, and the other's regex could not see the
    tile URL because it sits inside a JSON block rather than an attribute.
    """

    def test_no_code_is_loaded_from_a_network(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        for page in (rendered.index, *rendered.day_pages):
            for ref in re.findall(r'(?:src|href)="([^"]+)"', page.read_text()):
                if ref.endswith((".js", ".css")):
                    assert not ref.startswith(("http://", "https://")), ref

    def test_leaflet_is_vendored_beside_the_report(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert (rendered.root / "vendor" / "leaflet.js").exists()
        assert (rendered.root / "vendor" / "leaflet.css").exists()

    def test_the_only_thing_fetched_is_tiles(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        external = {
            ref
            for page in (rendered.index, *rendered.day_pages)
            for ref in SRC_PATTERN.findall(page.read_text())
            if ref.startswith(("http://", "https://"))
        }
        assert all(ref.startswith("https://www.openstreetmap.org/") for ref in external), external

    def test_the_route_survives_without_javascript(self, seeded: StageContext) -> None:
        """The SVG is the no-JS fallback, so the day is still readable with scripts disabled."""
        rendered = render_report(_document(seeded), seeded.out_dir)
        text = rendered.day_pages[0].read_text()

        assert "<noscript><svg" in text
        assert "dot interpolated" in text

    def test_the_index_still_needs_no_javascript(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir)
        assert "<script" not in rendered.index.read_text().lower()


class TestMapData:
    def test_the_route_and_marks_are_emitted_as_json(self, seeded: StageContext) -> None:
        from story_book.export.report import map_data

        payload = json.loads(
            map_data(
                [[48.2, 16.3], [48.21, 16.31]],
                [{"lat": 48.2, "lon": 16.3, "interpolated": False, "label": "a"}],
            )
        )
        assert len(payload["route"]) == 2 and len(payload["marks"]) == 1

    def test_an_interpolated_mark_is_flagged_for_the_renderer(self) -> None:
        from story_book.export.report import map_data

        payload = json.loads(
            map_data([], [{"lat": 48.2, "lon": 16.3, "interpolated": True, "label": "a"}])
        )
        assert payload["marks"][0]["interpolated"] is True

    def test_a_label_cannot_close_the_script_tag(self) -> None:
        """Labels are filenames. A filename must never be able to become script."""
        from story_book.export.report import map_data

        payload = map_data(
            [], [{"lat": 48.2, "lon": 16.3, "interpolated": False, "label": "</script><b>x"}]
        )
        assert "</script>" not in payload


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


class TestStoryOverlay:
    """T44: a model's `story.json` fed back in. Words only -- never a source of structure."""

    def _story(self, seeded: StageContext) -> dict:
        document = _document(seeded)
        ids = [a["asset_id"] for a in document["assets"].values()]
        date = document["days"][0]["date"]
        return {
            "schema_version": 1,
            "title": "A Week in Vienna",
            "days": [{"date": date, "narrative": "We walked all morning.", "summary": "Walking."}],
            "chapters": [
                {
                    "chapter_id": "c1",
                    "date": date,
                    "title": "Into the old town",
                    "narrative": "The streets narrowed.",
                    "asset_ids": ids[:2],
                    "source_event_ids": [],
                    "uncertainties": ["The church is named from the photograph."],
                }
            ],
            "captions": [{"asset_id": ids[0], "caption": "The first square."}],
            "uncertainties": ["Personal reactions are inferred."],
        }

    def test_the_day_narrative_reaches_the_page(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        assert "We walked all morning." in rendered.day_pages[0].read_text()

    def test_a_chapter_and_its_narrative_render(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        text = rendered.day_pages[0].read_text()
        assert "Into the old town" in text and "The streets narrowed." in text

    def test_a_caption_renders_next_to_its_photo(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        assert "The first square." in rendered.day_pages[0].read_text()

    def test_a_chapter_uncertainty_is_surfaced(self, seeded: StageContext) -> None:
        """A hedge the reader never sees is the same as no hedge at all."""
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        assert "named from the photograph" in rendered.day_pages[0].read_text()

    def test_the_story_title_reaches_the_index(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        assert "A Week in Vienna" in rendered.index.read_text()

    def test_a_reference_to_media_not_in_the_trip_is_dropped_not_rendered(
        self, seeded: StageContext
    ) -> None:
        """A chapter cannot invent a page; the pipeline still decides what exists."""
        story = self._story(seeded)
        story["chapters"][0]["asset_ids"] = ["deadbeefdeadbeef"]

        rendered = render_report(_document(seeded), seeded.out_dir, story)
        assert "deadbeefdeadbeef" not in rendered.day_pages[0].read_text()

    def test_a_chapter_missing_optional_keys_still_renders(self, seeded: StageContext) -> None:
        """The real response used `time_range_local` and no `starts_at`; strict lookup crashed."""
        story = self._story(seeded)
        story["chapters"][0].pop("starts_at", None)
        story["chapters"][0].pop("narrative")
        story["chapters"][0]["time_range_local"] = {"start": "09:00", "end": "10:00"}

        rendered = render_report(_document(seeded), seeded.out_dir, story)
        assert "Into the old town" in rendered.day_pages[0].read_text()

    def test_a_chapter_with_no_title_does_not_render_the_word_none(
        self, seeded: StageContext
    ) -> None:
        story = self._story(seeded)
        story["chapters"][0].pop("title")

        assert "None" not in rendered_title(
            render_report(_document(seeded), seeded.out_dir, story).day_pages[0]
        )

    def test_without_a_story_the_report_is_unchanged(self, seeded: StageContext) -> None:
        plain = render_report(_document(seeded), seeded.out_dir).day_pages[0].read_text()
        assert "chapter" not in plain.lower().split("stops")[0]

    def test_every_referenced_file_still_exists_with_a_story(self, seeded: StageContext) -> None:
        rendered = render_report(_document(seeded), seeded.out_dir, self._story(seeded))
        missing = [
            ref
            for page in (rendered.index, *rendered.day_pages)
            for ref in SRC_PATTERN.findall(page.read_text())
            if not ref.startswith(("http://", "https://"))
            and not (page.parent / ref).resolve().exists()
        ]
        assert missing == []


def rendered_title(page: Path) -> str:
    text = page.read_text()
    start = text.find('<section class="chapter">')
    return text[start : start + 400]


class TestClipLength:
    def test_a_sub_second_clip_does_not_read_as_zero(self) -> None:
        from story_book.export.report import clip_length

        assert clip_length(0.37) == "<1s"

    def test_a_normal_clip_rounds_to_seconds(self) -> None:
        from story_book.export.report import clip_length

        assert clip_length(77.6) == "78s"

    def test_no_duration_renders_empty(self) -> None:
        from story_book.export.report import clip_length

        assert clip_length(None) == ""


class TestStoryOverlayToleratesRealResponses:
    """The renderer is generous with what it got; `check-story` is strict about the shape."""

    def test_a_day_using_summary_instead_of_narrative_still_renders(
        self, seeded: StageContext
    ) -> None:
        """The real response used `title` and `summary`; the contract asks for `narrative`.
        Throwing away four good paragraphs over a key name helps nobody."""
        document = _document(seeded)
        date = document["days"][0]["date"]
        story = {
            "schema_version": 1,
            "days": [{"date": date, "title": "Klimt at the Belvedere", "summary": "A museum day."}],
            "chapters": [],
            "captions": [],
            "uncertainties": [],
        }
        text = render_report(document, seeded.out_dir, story).day_pages[0].read_text()

        assert "Klimt at the Belvedere" in text and "A museum day." in text

    def test_the_empty_and_populated_branches_expose_the_same_keys(self) -> None:
        """A key present in one branch and absent in the other is a failed render under
        StrictUndefined -- which caught this, but only after 17 tests went red."""
        from story_book.export.report import _story_for

        empty = _story_for(None, "2026-07-18")
        populated = _story_for(
            {"days": [{"date": "2026-07-18", "narrative": "x"}], "chapters": []}, "2026-07-18"
        )
        assert set(empty) == set(populated)


class TestStoryDirectoryIsNeverDestroyed:
    """The one thing in `--out` that a rebuild cannot recreate.

    Everything else there is derived from the photographs. `story.json` and the trip context came
    back from a chat, and the prose beside them is editorial judgement -- so "delete the output and
    rebuild" has to stay true for the derived parts without taking these with it.
    """

    def test_rendering_the_report_leaves_it_alone(self, seeded: StageContext) -> None:
        story_dir = seeded.out_dir / "story"
        story_dir.mkdir()
        kept = story_dir / "editorial_notes.md"
        kept.write_text("hand-written")

        render_report(_document(seeded), seeded.out_dir)
        assert kept.read_text() == "hand-written"

    def test_building_the_package_leaves_it_alone(self, seeded: StageContext) -> None:
        from story_book.export.package import build_package

        story_dir = seeded.out_dir / "story"
        story_dir.mkdir()
        kept = story_dir / "story.json"
        kept.write_text('{"schema_version": 1}')

        build_package(_document(seeded), seeded.out_dir)
        assert kept.read_text() == '{"schema_version": 1}'


class TestMediaPrefixIsAParameter:
    """XT-1, from the iOS tracker's I25.

    The app renders *this* report with a custom scheme and resolves each image against the
    phone's originals. The alternative was rewriting generated HTML in the app, which is
    unbounded work and drifts the moment a template changes.

    The load-bearing test is the first one: the default has to stay byte-identical, or this
    stopped being a parameter and became a change.
    """

    def _render_to(self, seeded: StageContext, name: str, **kwargs) -> dict[str, str]:
        out = seeded.out_dir / name
        out.mkdir()
        rendered = render_report(_document(seeded), out, **kwargs)
        return {
            page.relative_to(rendered.root).as_posix(): page.read_text()
            for page in [rendered.index, *rendered.day_pages]
        }

    def test_omitting_the_prefix_matches_passing_the_default(self, seeded: StageContext) -> None:
        """Only the `None` wiring. This deliberately does *not* prove the default is unchanged:
        both sides read the same constant, so it passed happily with the default broken to
        `./`. Kept because it is the cheapest guard on the argument being threaded through at
        all, and paired with the test below, which is the one that can see a change."""
        without = self._render_to(seeded, "plain")
        explicit = self._render_to(seeded, "explicit", media_prefix=MediaPrefix())
        assert without == explicit

    def test_the_default_prefixes_are_still_the_relative_pair(self, seeded: StageContext) -> None:
        """The claim XT-1 actually makes: adding the parameter changed no existing output.

        Asserted against the literal prefixes rather than against another render, because a
        comparison of the default with itself cannot fail. Two depths, because the index and the
        day pages differ and a single-prefix regression would only show on one of them.
        """
        pages = self._render_to(seeded, "default_literal")
        assert 'src="../thumbs/' in pages["index.html"]
        day = next(html for name, html in pages.items() if name.startswith("days/"))
        assert 'src="../../thumbs/' in day

    def test_a_custom_scheme_reaches_the_index(self, seeded: StageContext) -> None:
        pages = self._render_to(
            seeded, "scheme", media_prefix=MediaPrefix.absolute("storyasset://")
        )
        assert 'src="storyasset://' in pages["index.html"]

    def test_a_custom_scheme_reaches_the_day_pages(self, seeded: StageContext) -> None:
        """The day pages sit one level deeper, so a prefix that only reached the index would
        look like it worked while every image on the page a reader actually opens was wrong."""
        pages = self._render_to(
            seeded, "scheme_days", media_prefix=MediaPrefix.absolute("storyasset://")
        )
        day = next(html for name, html in pages.items() if name.startswith("days/"))
        assert 'src="storyasset://' in day

    def test_a_custom_scheme_leaves_no_relative_media_reference_behind(
        self, seeded: StageContext
    ) -> None:
        """The control. A prefix applied to the thumbnail but not the tap-through `<a href>`
        would pass every check above and give the reader a dead link on every photograph."""
        pages = self._render_to(
            seeded, "scheme_all", media_prefix=MediaPrefix.absolute("storyasset://")
        )
        stragglers = [
            (name, ref)
            for name, html in pages.items()
            for ref in SRC_PATTERN.findall(html)
            if "thumbs/" in ref or "previews/" in ref
            if not ref.startswith("storyasset://")
        ]
        assert stragglers == []

    def test_the_stylesheet_and_vendored_code_are_not_media(self, seeded: StageContext) -> None:
        """`media_rel` prefixes media, not the report's own files. Sending `style.css` through
        a scheme handler that resolves asset ids would unstyle the page."""
        pages = self._render_to(
            seeded, "scheme_assets", media_prefix=MediaPrefix.absolute("storyasset://")
        )
        assert 'href="style.css"' in pages["index.html"]
        assert "storyasset://style.css" not in pages["index.html"]
