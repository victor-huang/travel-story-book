"""The ChatGPT package, against a real DB and real fixture media.

Structured around P02's seven requirements, because those are what the module exists to satisfy.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from PIL import Image

from story_book.db import connection as db
from story_book.db.models import MediaKind
from story_book.export.package import (
    MANIFEST_SCHEMA_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    ORIGINALS,
    PREVIEW,
    build_manifest,
    build_package,
    write_archive,
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
        # A real poster file on disk: without one the asset has nothing to export and the video
        # path under test is never exercised.
        poster = ctx.out_dir / "cache" / "poster.jpg"
        poster.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), "grey").save(poster, format="JPEG")
        frames = []
        for index in range(5):
            frame = ctx.out_dir / "cache" / f"frame{index}.jpg"
            Image.new("RGB", (64, 48), "grey").save(frame, format="JPEG")
            frames.append(str(frame.relative_to(ctx.out_dir)))
        ctx.conn.execute(
            "INSERT INTO video_meta (media_hash, fps, poster_path, keyframe_paths, motion_score,"
            " mean_volume_db, has_speech) VALUES (?, 30.0, ?, ?, 0.3, -30.0, ?)",
            (
                media_hash,
                str(poster.relative_to(ctx.out_dir)),
                json.dumps(frames),
                int(text is not None),
            ),
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


class TestManifestValidatesAgainstItsShippedSchema:
    """P05 asked for a schema so a consumer can validate before trusting a package."""

    def test_the_schema_travels_inside_the_package(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert (built.root / "schema" / MANIFEST_SCHEMA_FILENAME).exists()

    def test_the_schema_itself_is_valid(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        schema = json.loads((built.root / "schema" / MANIFEST_SCHEMA_FILENAME).read_text())
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_the_manifest_validates(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        schema = json.loads((built.root / "schema" / MANIFEST_SCHEMA_FILENAME).read_text())
        jsonschema.validate(json.loads(built.manifest.read_text()), schema)

    def test_a_manifest_with_video_validates(self, seeded: StageContext, make_media) -> None:
        _add_video(seeded, make_media, processed=True, text="hello")
        built = build_package(_document(seeded), seeded.out_dir)
        schema = json.loads((built.root / "schema" / MANIFEST_SCHEMA_FILENAME).read_text())
        jsonschema.validate(json.loads(built.manifest.read_text()), schema)


class TestCapturedVersusIncluded:
    """`assets` holds the selected subset; one count for both invited the wrong conclusion."""

    def test_captured_and_included_are_separate_numbers(self, seeded: StageContext) -> None:
        counts = build_manifest(_document(seeded), PREVIEW)["days"][0]["counts"]
        assert {"captured", "included"} == set(counts)

    def test_included_matches_the_number_of_records(self, seeded: StageContext) -> None:
        day = build_manifest(_document(seeded), PREVIEW)["days"][0]
        assert day["counts"]["included"]["media"] == len(day["assets"])

    def test_the_scope_is_stated_explicitly(self, seeded: StageContext) -> None:
        assert build_manifest(_document(seeded), PREVIEW)["days"][0]["asset_scope"] == (
            "selected_only"
        )

    def test_the_brief_reports_both_numbers(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        text = built.days[0].brief.read_text()
        assert "captured" in text and "included in this package" in text


class TestTimestampsAreUnambiguous:
    def test_local_time_carries_its_offset(self, seeded: StageContext) -> None:
        seeded.conn.execute("UPDATE media SET tz_offset_minutes = 120, tz_name = 'Europe/Vienna'")
        seeded.conn.commit()
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert record["taken_local"].endswith("+02:00")

    def test_the_utc_instant_is_present(self, seeded: StageContext) -> None:
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert record["taken_utc"]

    def test_the_iana_zone_is_present(self, seeded: StageContext) -> None:
        seeded.conn.execute("UPDATE media SET tz_name = 'Europe/Vienna'")
        seeded.conn.commit()
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert record["timezone"] == "Europe/Vienna"

    def test_the_day_reports_its_zone(self, seeded: StageContext) -> None:
        seeded.conn.execute("UPDATE media SET tz_name = 'Europe/Vienna'")
        seeded.conn.commit()
        assert build_manifest(_document(seeded), PREVIEW)["days"][0]["timezone"] == "Europe/Vienna"


class TestLayoutGeometry:
    def test_every_record_reports_orientation_and_aspect(self, seeded: StageContext) -> None:
        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        assert records and all(
            {"width", "height", "orientation", "aspect_ratio"} <= set(a) for a in records
        )

    def test_a_landscape_photo_is_labelled_landscape(self, seeded: StageContext) -> None:
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert record["orientation"] == "landscape"


class TestEmptyStopsAreListed:
    def test_a_stop_with_nothing_selected_still_appears(self, seeded: StageContext) -> None:
        """Omitting it makes the day read as continuous when it was not."""
        manifest = build_manifest(_document(seeded), PREVIEW)
        day = manifest["days"][0]
        day["events"].append(
            {
                "event_id": "2026-07-18#9",
                "event_type": "detected_cluster",
                "label": None,
                "place": "Somewhere else",
                "start_local": "2026-07-18T20:00:00",
                "end_local": "2026-07-18T20:05:00",
                "duration_seconds": 300,
                "duration_display": "5 min",
                "counts": {"media": 4, "images": 4, "videos": 0},
                "location": {
                    "centroid": None,
                    "start": None,
                    "end": None,
                    "radius_m": None,
                    "gps_coverage": 0.0,
                    "moved": False,
                },
                "landmarks": [],
                "asset_ids": [],
            }
        )
        from story_book.export.package import _render_brief

        text = _render_brief(manifest, day)
        assert "Somewhere else" in text and "No photograph from this stop" in text

    def test_the_count_of_unrepresented_stops_is_stated(self, seeded: StageContext) -> None:
        manifest = build_manifest(_document(seeded), PREVIEW)
        day = manifest["days"][0]
        day["events"].append(
            {
                "event_id": "2026-07-18#9",
                "event_type": "detected_cluster",
                "label": None,
                "place": None,
                "start_local": None,
                "end_local": None,
                "duration_seconds": None,
                "duration_display": "",
                "counts": {"media": 1, "images": 1, "videos": 0},
                "location": {
                    "centroid": None,
                    "start": None,
                    "end": None,
                    "radius_m": None,
                    "gps_coverage": 0.0,
                    "moved": False,
                },
                "landmarks": [],
                "asset_ids": [],
            }
        )
        from story_book.export.package import _render_brief

        assert "have no photograph in this package" in _render_brief(manifest, day)


class TestEventsAreNotChapters:
    def test_an_event_declares_itself_a_detected_cluster(self, seeded: StageContext) -> None:
        events = build_manifest(_document(seeded), PREVIEW)["days"][0]["events"]
        assert all(e["event_type"] == "detected_cluster" for e in events)

    def test_the_prompt_asks_the_model_to_draw_chapters(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "source_event_ids" in built.days[0].prompt.read_text()


class TestSelectionReasons:
    def test_a_pinned_asset_says_a_human_chose_it(self, seeded: StageContext) -> None:
        from story_book.overrides import Overrides

        first = seeded.conn.execute("SELECT path FROM media LIMIT 1").fetchone()["path"]
        pinned = replace(seeded, overrides=Overrides.from_dict({"pin": [Path(first).name]}))
        SelectionStage().run(pinned)

        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        assert any("human_pinned" in a["selection"]["reasons"] for a in records)

    def test_an_unpinned_highlight_says_it_was_ranked(self, seeded: StageContext) -> None:
        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        assert any("quality_ranked" in a["selection"]["reasons"] for a in records)

    def test_a_video_exported_only_for_the_storyboard_says_so(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        video = next(a for a in records if a["kind"] == "video")
        assert video["selection"]["reasons"]

    def test_rank_within_the_day_is_reported(self, seeded: StageContext) -> None:
        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        assert any(a["selection"]["rank_within_day"] for a in records)


class TestPlaceCertainty:
    def test_the_geocoder_source_is_named(self, seeded: StageContext) -> None:
        seeded.conn.execute(
            "INSERT INTO place (id, lat_key, lon_key, city, country, source) "
            "VALUES (1, 48.21, 16.37, 'Vienna', 'AT', 'offline')"
        )
        seeded.conn.execute("UPDATE media SET place_id = 1")
        seeded.conn.commit()
        record = next(
            a
            for d in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in d["assets"]
            if a["place"]
        )
        assert record["place"]["source"] == "offline"

    def test_no_confidence_number_is_invented(self, seeded: StageContext) -> None:
        """The offline geocoder reports none; a fabricated figure is worse than a stated limit."""
        seeded.conn.execute(
            "INSERT INTO place (id, lat_key, lon_key, city, country, source) "
            "VALUES (1, 48.21, 16.37, 'Vienna', 'AT', 'offline')"
        )
        seeded.conn.execute("UPDATE media SET place_id = 1")
        seeded.conn.commit()
        record = next(
            a
            for d in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in d["assets"]
            if a["place"]
        )
        assert "confidence" not in record["place"]
        assert record["place"]["precision"] == "city"

    def test_the_prompt_permits_flagged_visual_landmark_inference(
        self, seeded: StageContext
    ) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        text = built.days[0].prompt.read_text()
        assert "may name a landmark you recognise" in text and "uncertainties" in text


class TestVideoStoryboardData:
    def test_keyframes_carry_their_offset_into_the_clip(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        records = [
            a for d in build_manifest(_document(seeded), PREVIEW)["days"] for a in d["assets"]
        ]
        video = next(a for a in records if a["kind"] == "video")
        assert all("seconds" in frame for frame in video["video"]["keyframes"])

    def test_the_prompt_asks_for_a_source_range_not_just_a_duration(
        self, seeded: StageContext
    ) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        text = built.days[0].prompt.read_text()
        assert "source_start_seconds" in text and "source_end_seconds" in text


class TestCleanArchive:
    def test_it_writes_a_zip(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert write_archive(built).exists()

    def test_macos_droppings_are_excluded(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        (built.root / ".DS_Store").write_bytes(b"junk")
        (built.days[0].directory / "._IMG_1.jpeg").write_bytes(b"junk")

        with zipfile.ZipFile(write_archive(built)) as archive:
            names = archive.namelist()
        assert not any(".DS_Store" in n or "/._" in n for n in names)

    def test_the_manifest_is_in_the_archive(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        with zipfile.ZipFile(write_archive(built)) as archive:
            assert any(n.endswith("manifest.json") for n in archive.namelist())

    def test_rewriting_replaces_rather_than_appends(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        first = write_archive(built)
        count = len(zipfile.ZipFile(first).namelist())
        assert len(zipfile.ZipFile(write_archive(built)).namelist()) == count


class TestExportedFilesAreWhatTheyClaim:
    """P06's critical finding: video exports were JPEGs written under `.mov` names.

    The manifest advertised nine playable clips and shipped nine still images. A consumer decoding
    them fails; one that trusts the manifest believes it has footage it does not have.
    """

    def test_a_video_poster_is_named_and_typed_as_an_image(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        video = next(a for day in manifest["days"] for a in day["assets"] if a["kind"] == "video")
        assert video["export_path"].endswith("_poster.jpg")
        assert video["export_media_type"] == "image/jpeg"
        assert video["export_role"] == "poster_frame"

    def test_no_export_path_carries_a_video_extension_without_video_content(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        for day in manifest["days"]:
            for a in day["assets"]:
                if a["export_media_type"] == "image/jpeg":
                    assert not a["export_path"].lower().endswith((".mov", ".mp4", ".m4v"))

    def test_a_preview_only_package_says_no_proxy_is_included(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        manifest = build_manifest(_document(seeded), PREVIEW, False)
        assert manifest["package"]["video_proxies_included"] is False
        assert "No playable video" in manifest["package"]["video_note"]

    def test_a_photo_export_keeps_its_own_filename(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        manifest = json.loads(built.manifest.read_text())
        image = next(a for day in manifest["days"] for a in day["assets"] if a["kind"] == "image")
        assert image["source_filename"] in image["export_path"]


class TestShortClipsAreMarked:
    def test_a_sub_two_second_clip_is_labelled_short(
        self, seeded: StageContext, make_media
    ) -> None:
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET duration = 0.4 WHERE hash = ?", (media_hash,))
        seeded.conn.commit()
        video = next(
            a
            for day in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in day["assets"]
            if a["kind"] == "video"
        )
        assert video["video"]["subtype"] == "short_clip"

    def test_a_short_clip_is_not_a_storyboard_candidate(
        self, seeded: StageContext, make_media
    ) -> None:
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET duration = 0.4 WHERE hash = ?", (media_hash,))
        seeded.conn.commit()
        video = next(
            a
            for day in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in day["assets"]
            if a["kind"] == "video"
        )
        assert video["video"]["storyboard_candidate"] is False

    def test_a_short_clip_ships_one_keyframe_not_five(
        self, seeded: StageContext, make_media
    ) -> None:
        """Five frames sampled across 0.4 seconds are five views of one instant."""
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET duration = 0.4 WHERE hash = ?", (media_hash,))
        seeded.conn.commit()
        video = next(
            a
            for day in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in day["assets"]
            if a["kind"] == "video"
        )
        assert len(video["video"]["keyframes"]) <= 1

    def test_a_normal_clip_remains_a_candidate(self, seeded: StageContext, make_media) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        video = next(
            a
            for day in build_manifest(_document(seeded), PREVIEW)["days"]
            for a in day["assets"]
            if a["kind"] == "video"
        )
        assert video["video"]["storyboard_candidate"] is True

    def test_the_brief_flags_a_short_clip(self, seeded: StageContext, make_media) -> None:
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET duration = 0.4 WHERE hash = ?", (media_hash,))
        seeded.conn.commit()
        built = build_package(_document(seeded), seeded.out_dir)
        assert "too short for a storyboard" in built.days[0].brief.read_text()


class TestPromptDoesNotManufacturePrecision:
    def test_without_proxies_ranges_are_called_estimates(
        self, seeded: StageContext, make_media
    ) -> None:
        """Five stills from a 112-second clip cannot support a confident choice of seconds 43-51."""
        _add_video(seeded, make_media, processed=True, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        text = built.days[0].prompt.read_text()
        assert "No playable footage is included" in text and "estimates" in text

    def test_without_proxies_the_model_is_told_to_anchor_to_a_keyframe(
        self, seeded: StageContext, make_media
    ) -> None:
        _add_video(seeded, make_media, processed=True, text=None)
        built = build_package(_document(seeded), seeded.out_dir)
        assert "anchor them" in built.days[0].prompt.read_text()

    def test_a_day_with_no_footage_says_so(self, seeded: StageContext) -> None:
        built = build_package(_document(seeded), seeded.out_dir)
        assert "no footage" in built.days[0].prompt.read_text()


class TestAfterMidnightIsExplicit:
    def test_an_asset_reports_the_calendar_date_it_was_taken_on(
        self, seeded: StageContext, make_media
    ) -> None:
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert record["calendar_date"] == "2026-07-18"

    def test_the_day_assignment_rule_is_stated(self, seeded: StageContext) -> None:
        rule = build_manifest(_document(seeded), PREVIEW)["trip"]["day_assignment_rule"]
        assert "04:00" in rule

    def test_a_past_midnight_stop_shows_its_calendar_date(self, seeded: StageContext) -> None:
        from story_book.export.package import _stamp

        after = {"taken_local": "2026-07-20T00:59:12+02:00", "calendar_date": "2026-07-20"}
        assert _stamp(after, "2026-07-19") == "00:59 (2026-07-20)"

    def test_a_same_day_stop_shows_only_the_clock(self, seeded: StageContext) -> None:
        from story_book.export.package import _stamp

        same = {"taken_local": "2026-07-19T15:46:00+02:00", "calendar_date": "2026-07-19"}
        assert _stamp(same, "2026-07-19") == "15:46"


class TestNoDuplicatedState:
    def test_pinned_by_human_lives_only_inside_selection(self, seeded: StageContext) -> None:
        """Two places for one fact is one place eventually wrong."""
        record = build_manifest(_document(seeded), PREVIEW)["days"][0]["assets"][0]
        assert "pinned_by_human" not in record
        assert "pinned_by_human" in record["selection"]


class TestMachineReadableDurations:
    def test_an_event_reports_seconds_and_a_display_string(self, seeded: StageContext) -> None:
        event = build_manifest(_document(seeded), PREVIEW)["days"][0]["events"][0]
        assert isinstance(event["duration_seconds"], int)
        assert isinstance(event["duration_display"], str)

    def test_the_two_agree(self, seeded: StageContext) -> None:
        event = build_manifest(_document(seeded), PREVIEW)["days"][0]["events"][0]
        assert f"{event['duration_seconds'] // 60} min" == event["duration_display"]


class TestTripBoundsAreUnambiguous:
    def test_the_trip_start_carries_its_offset(self, seeded: StageContext) -> None:
        seeded.conn.execute("UPDATE media SET tz_offset_minutes = 120")
        seeded.conn.commit()
        assert build_manifest(_document(seeded), PREVIEW)["trip"]["start_local"].endswith("+02:00")

    def test_utc_bounds_are_reported(self, seeded: StageContext) -> None:
        trip = build_manifest(_document(seeded), PREVIEW)["trip"]
        assert trip["start_utc"] and trip["end_utc"]

    def test_bounds_are_ordered_by_utc_not_by_the_local_string(self) -> None:
        """`...T09:00+02:00` sorts after `...T08:00+01:00` while being the earlier instant."""
        from story_book.pipeline.timeline import _trip_bound

        assets = {
            "a": {
                "taken_utc": "2026-07-18T07:00:00+00:00",
                "taken_local": "2026-07-18T09:00:00+02:00",
            },
            "b": {
                "taken_utc": "2026-07-18T06:00:00+00:00",
                "taken_local": "2026-07-18T07:00:00+01:00",
            },
        }
        assert _trip_bound(assets, "min") == "2026-07-18T07:00:00+01:00"


class TestVideoProxies:
    """The mode that makes an exact source range answerable rather than invented."""

    @pytest.mark.needs_ffmpeg
    def test_a_proxy_is_a_real_playable_mp4(
        self, seeded: StageContext, make_media, media_dir: Path
    ) -> None:
        clip = next((p for p in media_dir.glob("*.mov")), None)
        assert clip is not None, "fixture video missing"
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET path = ? WHERE hash = ?", (str(clip), media_hash))
        seeded.conn.commit()

        document = _document(seeded)
        sources = {
            a["asset_id"]: Path(
                seeded.conn.execute(
                    "SELECT path FROM media WHERE hash = ?", (a["content_hash"],)
                ).fetchone()["path"]
            )
            for a in document["assets"].values()
        }
        built = build_package(document, seeded.out_dir, source_for=sources, video_proxies=True)
        manifest = json.loads(built.manifest.read_text())
        video = next(a for d in manifest["days"] for a in d["assets"] if a["kind"] == "video")

        assert video["export_media_type"] == "video/mp4"
        assert video["export_role"] == "video_proxy"
        assert (built.root / video["export_path"]).read_bytes()[4:8] == b"ftyp"

    @pytest.mark.needs_ffmpeg
    def test_the_poster_is_kept_alongside_the_proxy(
        self, seeded: StageContext, make_media, media_dir: Path
    ) -> None:
        clip = next((p for p in media_dir.glob("*.mov")), None)
        assert clip is not None, "fixture video missing"
        media_hash = _add_video(seeded, make_media, processed=True, text=None)
        seeded.conn.execute("UPDATE media SET path = ? WHERE hash = ?", (str(clip), media_hash))
        seeded.conn.commit()

        document = _document(seeded)
        sources = {
            a["asset_id"]: Path(
                seeded.conn.execute(
                    "SELECT path FROM media WHERE hash = ?", (a["content_hash"],)
                ).fetchone()["path"]
            )
            for a in document["assets"].values()
        }
        built = build_package(document, seeded.out_dir, source_for=sources, video_proxies=True)
        manifest = json.loads(built.manifest.read_text())
        video = next(a for d in manifest["days"] for a in d["assets"] if a["kind"] == "video")

        assert (built.root / video["poster_path"]).exists()

    def test_a_failed_transcode_falls_back_to_the_poster_honestly(
        self, seeded: StageContext, make_media, mocker
    ) -> None:
        """Better a truthful poster than a manifest claiming footage that is not there."""
        _add_video(seeded, make_media, processed=True, text=None)
        mocker.patch("story_book.export.package._transcode_proxy", return_value=False)

        built = build_package(_document(seeded), seeded.out_dir, video_proxies=True)
        manifest = json.loads(built.manifest.read_text())
        video = next(a for d in manifest["days"] for a in d["assets"] if a["kind"] == "video")

        assert video["export_media_type"] == "image/jpeg"
        assert video["video_proxy_included"] is False
        assert any("proxy transcode failed" in reason for _, reason in built.skipped)
