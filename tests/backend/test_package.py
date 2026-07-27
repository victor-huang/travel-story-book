"""The ChatGPT package, against a real DB and real fixture media.

Structured around P02's seven requirements, because those are what the module exists to satisfy.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.db.models import MediaKind
from story_book.export.package import (
    MANIFEST_SCHEMA_VERSION,
    ORIGINALS,
    PREVIEW,
    build_manifest,
    build_package,
)
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.home_filter import HomeFilterStage
from story_book.pipeline.selection import SelectionStage
from story_book.pipeline.thumbnails import ThumbnailStage
from story_book.pipeline.timeline import build_timeline
from story_book.trip_context import Traveler, TripContext

VIENNA = (48.2082, 16.3738)


@pytest.fixture
def seeded(ctx: StageContext, make_media, media_dir: Path) -> StageContext:
    sources = sorted(media_dir.glob("*.jpg"))
    assert sources, "fixture media missing -- run tests/fixtures/generate.py"

    start = datetime(2026, 7, 18, 9)
    for index, source in enumerate(sources[:6]):
        at = start + timedelta(minutes=8 * index)
        media_hash = f"{index:064x}"
        db.upsert_media(
            ctx.conn,
            make_media(
                media_hash,
                path=str(source),
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0] + 0.0005 * index,
                lon=VIENNA[1],
                width=800,
                height=600,
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, face_count, "
            "overall, content_class) VALUES (?, 0.8, 0.7, 0.5, ?, ?, 'landscape')",
            (media_hash, index, 0.9 - index * 0.02),
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


def _add_video(ctx: StageContext, make_media, *, processed: bool, text: str | None) -> str:
    media_hash = "v" * 64
    at = datetime(2026, 7, 18, 9, 20)
    db.upsert_media(
        ctx.conn,
        make_media(
            media_hash,
            path="/src/CLIP.mov",
            kind=MediaKind.VIDEO,
            duration=31.0,
            taken_local=at.isoformat(),
            taken_utc=at.isoformat(),
            lat=VIENNA[0],
            lon=VIENNA[1],
        ),
    )
    if processed:
        ctx.conn.execute(
            "INSERT INTO video_meta (media_hash, fps, poster_path, keyframe_paths, motion_score,"
            " mean_volume_db, has_speech) VALUES (?, 30.0, NULL, '[]', 0.3, -30.0, ?)",
            (media_hash, int(text is not None)),
        )
        ctx.conn.execute(
            "INSERT INTO stage_result (media_hash, stage, stage_version, status, computed_at) "
            "VALUES (?, 'video', 1, 'ok', '2026-07-18T00:00:00')",
            (media_hash,),
        )
    if text is not None:
        ctx.conn.execute(
            "INSERT INTO transcript (media_hash, model, text, segments) VALUES (?,'small',?,NULL)",
            (media_hash, text),
        )
    ctx.conn.commit()
    DaysStage().run(ctx)
    EventStage().run(ctx)
    SelectionStage().run(ctx)
    return media_hash


def _document(ctx: StageContext, context: TripContext | None = None) -> dict:
    return build_timeline(ctx.conn, ctx.config, context, ctx.out_dir)


class TestManifestIsAuthoritative:
    def test_it_carries_a_schema_version(self, seeded: StageContext) -> None:
        assert build_manifest(_document(seeded), PREVIEW)["schema_version"] == (
            MANIFEST_SCHEMA_VERSION
        )

    def test_every_asset_record_has_a_stable_id_and_its_content_hash(
        self, seeded: StageContext
    ) -> None:
        manifest = build_manifest(_document(seeded), PREVIEW)
        records = [a for day in manifest["days"] for a in day["assets"]]
        assert records and all(a["content_hash"].startswith(a["asset_id"]) for a in records)

    def test_the_cell_id_is_an_attribute_not_the_identity(self, seeded: StageContext) -> None:
        """Cell ids are positional. The manifest keys on asset_id and records the cell beside it."""
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        records = [a for day in manifest["days"] for a in day["assets"]]
        assert all("cell_id" in a and "asset_id" in a for a in records)

    def test_cells_are_filled_in_after_the_sheets_render(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        records = [a for day in manifest["days"] for a in day["assets"]]
        assert any(a["cell_id"] for a in records)

    def test_the_brief_is_generated_from_the_manifest(self, seeded: StageContext) -> None:
        """Not maintained alongside it -- every id in the brief must come from a record."""
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        known = {a["asset_id"] for day in manifest["days"] for a in day["assets"]}
        brief = built.days[0].brief.read_text()
        assert all(asset_id in brief for asset_id in known)


class TestVideoRecords:
    def test_a_silent_clip_is_reported_as_a_measured_negative(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        assert "no speech found" in built.days[0].brief.read_text()

    def test_an_unprocessed_clip_is_not_reported_as_silent(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=False, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        assert "not analysed for speech" in built.days[0].brief.read_text()

    def test_a_transcript_reaches_the_brief(self, seeded: StageContext, make_media) -> None:
        _add_video(seeded, make_media, processed=True, text="the bells are ringing")
        built = build_package(_document(seeded), seeded.out_dir)
        assert "the bells are ringing" in built.days[0].brief.read_text()

    def test_a_sub_second_clip_does_not_read_as_zero_seconds(
        self, seeded: StageContext, make_media
    ) -> None:
        """`0s` looks like missing data rather than a very short clip."""
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET duration = 0.4 WHERE hash = ?", (media_hash,))
        seeded.conn.commit()
        built = build_package(_document(seeded), seeded.out_dir)
        text = built.days[0].brief.read_text()
        assert "<1s" in text and " 0s " not in text

    def test_every_video_is_packaged_even_when_it_won_no_highlight_slot(
        self, seeded: StageContext, make_media
    ) -> None:
        """A storyboard cannot reference footage the package never mentioned."""
        _add_video(seeded, make_media, processed=True, text=None)
        manifest = build_manifest(_document(seeded), PREVIEW)
        kinds = [a["kind"] for day in manifest["days"] for a in day["assets"]]
        assert "video" in kinds


class TestPlacesAreNamed:
    def test_the_country_code_is_expanded_to_a_name(self, seeded: StageContext) -> None:
        seeded.conn.execute(
            "INSERT INTO place (id, lat_key, lon_key, city, country, source) "
            "VALUES (1, 48.21, 16.37, 'Vienna', 'AT', 'offline')"
        )
        seeded.conn.execute("UPDATE media SET place_id = 1")
        seeded.conn.commit()
        manifest = build_manifest(_document(seeded), PREVIEW)
        records = [a for day in manifest["days"] for a in day["assets"] if a["place"]]
        assert records and all(a["place"]["country"] == "Austria" for a in records)

    def test_no_raw_coordinate_appears_in_the_brief(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "48.208" not in built.days[0].brief.read_text()


class TestTripContext:
    def test_absent_context_is_stated_explicitly_in_the_prompt(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "No personal context was supplied" in built.days[0].prompt.read_text()

    def test_absent_context_instructs_the_model_not_to_invent_feelings(
        self, seeded: StageContext
    ) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "Do not invent reactions" in built.days[0].prompt.read_text()

    def test_supplied_context_reaches_the_prompt(self, seeded: StageContext) -> None:
        context = TripContext(
            journal_voice="first_person_plural",
            travelers=(Traveler(role="partner", name="Sam"),),
            notes=("The concert was why we came.",),
        )
        built = build_package(_document(seeded, context), seeded.out_dir)
        text = built.days[0].prompt.read_text()
        assert "Sam" in text and "The concert was why we came." in text


class TestStructuredOutputIsRequested:
    @pytest.mark.parametrize(
        "key",
        [
            "chapters",
            "captions",
            "layout_pages",
            "video_scenes",
            "uncertainties",
            "requested_additional_context",
        ],
    )
    def test_the_prompt_asks_for_each_required_key(self, seeded: StageContext, key: str) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert f'"{key}"' in built.days[0].prompt.read_text()

    def test_the_prompt_asks_for_a_video_storyboard(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "storyboard" in built.days[0].prompt.read_text().lower()


class TestEventLocationIsRicherThanAPoint:
    def test_an_event_reports_extent_not_just_a_centroid(self, seeded: StageContext) -> None:
        manifest = build_manifest(_document(seeded), PREVIEW)
        location = manifest["days"][0]["events"][0]["location"]
        assert {"centroid", "start", "end", "radius_m", "gps_coverage"} <= set(location)


class TestQualityComponents:
    def test_components_reach_the_manifest(self, seeded: StageContext) -> None:
        manifest = build_manifest(_document(seeded), PREVIEW)
        quality = next(
            a["quality"] for day in manifest["days"] for a in day["assets"] if a["quality"]
        )
        assert {"sharpness", "exposure", "contrast", "faces_detected"} <= set(quality)

    def test_no_aesthetic_score_is_invented(self, seeded: StageContext) -> None:
        manifest = build_manifest(_document(seeded), PREVIEW)
        quality = next(
            a["quality"] for day in manifest["days"] for a in day["assets"] if a["quality"]
        )
        assert not {"aesthetic", "composition"} & set(quality)


class TestPreviewOrOriginals:
    def test_a_preview_package_says_so(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir, mode=PREVIEW)
        manifest = json.loads(built.manifest.read_text())
        assert "downscaled previews" in manifest["package"]["media_note"]

    def test_an_originals_package_says_so(self, seeded: StageContext, media_dir: Path) -> None:
        document = _document(seeded)
        sources = {
            a["asset_id"]: Path(
                seeded.conn.execute(
                    "SELECT path FROM media WHERE hash = ?", (a["content_hash"],)
                ).fetchone()["path"]
            )
            for a in document["assets"].values()
        }
        built = build_package(document, seeded.out_dir, mode=ORIGINALS, source_for=sources)
        manifest = json.loads(built.manifest.read_text())
        assert "Full-resolution originals" in manifest["package"]["media_note"]

    def test_the_note_reaches_the_brief_too(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir, mode=PREVIEW)
        assert "downscaled previews" in built.days[0].brief.read_text()

    def test_an_unknown_mode_is_refused(self, seeded: StageContext) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            build_package(_document(seeded), seeded.out_dir, mode="thumbnails")


class TestPackagedFiles:
    def test_a_day_directory_holds_a_sheet_a_brief_and_a_prompt(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        day = built.days[0]
        assert day.sheets and day.brief.exists() and day.prompt.exists()

    def test_a_readme_explains_the_upload_steps(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "attach" in (built.root / "README.md").read_text().lower()

    def test_a_stale_package_from_a_previous_run_is_replaced(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        stale = built.root / "1999-01-01"
        stale.mkdir()
        rebuilt = build_package(_document(seeded), seeded.out_dir)
        assert not (rebuilt.root / "1999-01-01").exists()

    def test_every_export_path_in_the_manifest_exists(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        paths = [
            built.root / a["export_path"]
            for day in manifest["days"]
            for a in day["assets"]
            if a["export_path"]
        ]
        assert paths and all(p.exists() for p in paths)

    def test_it_survives_a_trip_with_no_media(self, ctx: StageContext) -> None:
        built = build_package(build_timeline(ctx.conn, ctx.config), ctx.out_dir)
        assert built.manifest.exists() and built.days == ()
