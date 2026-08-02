"""Checking a model's `story.json` against the package it was written from.

Built after a real run came back richer than the contract and non-conformant: thirteen chapters
with no `source_event_ids`, `video_scenes` renamed, `layout_pages` absent. Every one of its 56
asset references resolved — so the two checks are genuinely separate, and a document can be
perfectly grounded and still unreadable by a renderer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.export.check_story import check_story
from story_book.export.package import PREVIEW, build_manifest
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.selection import SelectionStage
from story_book.pipeline.timeline import build_timeline

VIENNA = (48.2082, 16.3738)


@pytest.fixture
def manifest(ctx: StageContext, make_media) -> dict:
    start = datetime(2026, 7, 18, 9)
    for index in range(5):
        at = start + timedelta(minutes=10 * index)
        media_hash = f"{index:064x}"
        db.upsert_media(
            ctx.conn,
            make_media(
                media_hash,
                path=f"/src/IMG_{1000 + index}.jpeg",
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0],
                lon=VIENNA[1],
                width=4000,
                height=3000,
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, overall, "
            "content_class) VALUES (?, 0.8, 0.7, 0.5, ?, 'landscape')",
            (media_hash, 0.9 - index * 0.01),
        )
    ctx.conn.commit()
    DaysStage().run(ctx)
    EventStage().run(ctx)
    SelectionStage().run(ctx)
    return build_manifest(build_timeline(ctx.conn, ctx.config, None, ctx.out_dir), PREVIEW)


def _ids(manifest: dict) -> list[str]:
    return [a["asset_id"] for day in manifest["days"] for a in day["assets"]]


def _event(manifest: dict) -> str:
    return manifest["days"][0]["events"][0]["event_id"]


def _conformant(manifest: dict) -> dict:
    ids = _ids(manifest)
    return {
        "schema_version": 1,
        "days": [{"date": "2026-07-18", "narrative": "A day.", "summary": "A day."}],
        "chapters": [
            {
                "chapter_id": "c1",
                "date": "2026-07-18",
                "title": "Morning",
                "narrative": "We walked.",
                "asset_ids": ids,
                "source_event_ids": [_event(manifest)],
            }
        ],
        "captions": [{"asset_id": ids[0], "caption": "The square."}],
        "uncertainties": ["The church is named from the photograph, not from metadata."],
    }


class TestAConformantStoryPasses:
    def test_it_reports_ok(self, manifest: dict) -> None:
        assert check_story(_conformant(manifest), manifest).ok

    def test_no_shape_errors(self, manifest: dict) -> None:
        assert check_story(_conformant(manifest), manifest).schema_errors == []

    def test_full_coverage_is_reported(self, manifest: dict) -> None:
        report = check_story(_conformant(manifest), manifest)
        assert report.coverage == 1.0
        assert report.uncited_assets == []


class TestGroundingIsCheckedSeparatelyFromShape:
    def test_an_invented_asset_id_is_caught(self, manifest: dict) -> None:
        """A caption on an id the package lacks looks exactly like a fact."""
        story = _conformant(manifest)
        story["captions"].append({"asset_id": "deadbeefdeadbeef", "caption": "Nowhere."})

        report = check_story(story, manifest)
        assert [a for a, _ in report.unknown_assets] == ["deadbeefdeadbeef"]
        assert not report.ok

    def test_the_location_of_a_bad_reference_is_reported(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["captions"].append({"asset_id": "deadbeefdeadbeef", "caption": "Nowhere."})

        assert "captions" in check_story(story, manifest).unknown_assets[0][1]

    def test_references_are_found_wherever_the_model_put_them(self, manifest: dict) -> None:
        """The point of the check is that the model may not use the keys it was asked for."""
        story = _conformant(manifest)
        story["video_storyboard"] = {"scenes": [{"asset_ids": ["notarealassetid0"]}]}

        assert [a for a, _ in check_story(story, manifest).unknown_assets] == ["notarealassetid0"]

    def test_an_unknown_event_id_is_caught(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["chapters"][0]["source_event_ids"] = ["2099-01-01#7"]

        assert check_story(story, manifest).unknown_events == [("2099-01-01#7", "c1")]

    def test_a_chapter_citing_another_day_is_caught(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["chapters"][0]["date"] = "2026-07-19"

        report = check_story(story, manifest)
        assert report.cross_day_assets and not report.ok

    def test_an_uncited_asset_is_information_not_an_error(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["chapters"][0]["asset_ids"] = _ids(manifest)[:2]

        report = check_story(story, manifest)
        assert report.uncited_assets and report.ok

    def test_coverage_counts_distinct_assets(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["chapters"][0]["asset_ids"] = [_ids(manifest)[0]] * 4

        assert check_story(story, manifest).cited == 1


class TestShapeIsCheckedSeparatelyFromGrounding:
    def test_missing_source_event_ids_is_a_shape_error(self, manifest: dict) -> None:
        """The exact defect the first real run shipped, on all thirteen chapters."""
        story = _conformant(manifest)
        del story["chapters"][0]["source_event_ids"]

        report = check_story(story, manifest)
        assert any("source_event_ids" in e for e in report.schema_errors)
        assert not report.ok

    def test_a_renamed_key_is_a_shape_error(self, manifest: dict) -> None:
        story = _conformant(manifest)
        story["global_uncertainties"] = story.pop("uncertainties")

        assert any("uncertainties" in e for e in check_story(story, manifest).schema_errors)

    def test_a_grounded_but_misshapen_story_still_fails(self, manifest: dict) -> None:
        """100% of references resolving does not make a document a renderer can read."""
        story = _conformant(manifest)
        del story["chapters"][0]["source_event_ids"]

        report = check_story(story, manifest)
        assert report.coverage == 1.0
        assert report.unknown_assets == []
        assert not report.ok

    def test_absent_optional_sections_are_listed_not_failed(self, manifest: dict) -> None:
        report = check_story(_conformant(manifest), manifest)
        assert set(report.missing_optional) == {
            "layout_pages",
            "video_scenes",
            "requested_additional_context",
        }
        assert report.ok


class TestTheShippedSchemaTravelsWithThePackage:
    def test_the_story_schema_is_in_the_package(self, ctx: StageContext, manifest: dict) -> None:
        from story_book.export.package import STORY_SCHEMA_FILENAME, build_package

        built = build_package(build_timeline(ctx.conn, ctx.config, None, ctx.out_dir), ctx.out_dir)
        assert (built.root / "schema" / STORY_SCHEMA_FILENAME).exists()

    def test_the_story_schema_is_valid(self) -> None:
        import json

        import jsonschema

        from story_book.export.check_story import STORY_SCHEMA

        jsonschema.Draft202012Validator.check_schema(json.loads(Path(STORY_SCHEMA).read_text()))

    def test_the_prompt_points_at_it(self, ctx: StageContext, manifest: dict) -> None:
        from story_book.export.package import build_package

        built = build_package(build_timeline(ctx.conn, ctx.config, None, ctx.out_dir), ctx.out_dir)
        assert "story.schema.json" in built.days[0].prompt.read_text()
