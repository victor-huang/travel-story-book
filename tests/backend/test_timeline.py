"""`trip.json` against a real DB.

T31's acceptance: the document validates against its published schema and carries everything the
report (T40) and the package (T41) need, so neither has to reach back into the database.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from story_book.config import HomeLocation
from story_book.db import connection as db
from story_book.db.models import MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.home_filter import HomeFilterStage
from story_book.pipeline.selection import SelectionStage
from story_book.pipeline.timeline import (
    TRIP_JSON_FILENAME,
    TRIP_JSON_SCHEMA_VERSION,
    TimelineStage,
    TranscriptStatus,
    build_timeline,
)
from story_book.trip_context import Traveler, TripContext

SCHEMA_PATH = Path("src/story_book/trip_schema.json")
VIENNA = (48.2082, 16.3738)


@pytest.fixture(scope="module")
def schema() -> dict:
    """The published contract. Committed, so assert its presence rather than skipping."""
    assert SCHEMA_PATH.exists(), f"missing schema: {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text())


def _seed(ctx: StageContext, make_media, count: int = 8, *, minutes: float = 6.0) -> None:
    start = datetime(2026, 7, 18, 9)
    for index in range(count):
        at = start + timedelta(minutes=minutes * index)
        media_hash = f"{index:064x}"
        db.upsert_media(
            ctx.conn,
            make_media(
                media_hash,
                path=f"/src/IMG_{1000 + index}.jpeg",
                taken_local=at.isoformat(),
                taken_utc=at.isoformat(),
                lat=VIENNA[0] + 0.0004 * index,
                lon=VIENNA[1],
                width=4000,
                height=3000,
                tz_name="Europe/Vienna",
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, face_count, "
            "face_max_frac, overall, content_class) VALUES (?, ?, ?, ?, ?, ?, ?, 'landscape')",
            (media_hash, 0.8, 0.7, 0.5, 1, 0.01, 0.9 - index * 0.01),
        )
    ctx.conn.commit()


def _add_video(ctx: StageContext, make_media, *, processed: bool, transcript: str | None) -> str:
    media_hash = "v" * 64
    at = datetime(2026, 7, 18, 10, 30)
    db.upsert_media(
        ctx.conn,
        make_media(
            media_hash,
            path="/src/CLIP_1.mov",
            kind=MediaKind.VIDEO,
            duration=12.5,
            taken_local=at.isoformat(),
            taken_utc=at.isoformat(),
            lat=VIENNA[0],
            lon=VIENNA[1],
        ),
    )
    if processed:
        ctx.conn.execute(
            "INSERT INTO video_meta (media_hash, fps, poster_path, keyframe_paths, "
            "motion_score, mean_volume_db, has_speech) VALUES (?, 30.0, 'p.jpg', ?, 0.4, -22.0, ?)",
            (media_hash, json.dumps(["k1.jpg", "k2.jpg"]), int(transcript is not None)),
        )
        ctx.conn.execute(
            "INSERT INTO stage_result (media_hash, stage, stage_version, status, computed_at) "
            "VALUES (?, 'video', 1, 'ok', '2026-07-18T00:00:00')",
            (media_hash,),
        )
    if transcript is not None:
        ctx.conn.execute(
            "INSERT INTO transcript (media_hash, model, text, segments) VALUES (?, 'small', ?, ?)",
            (media_hash, transcript, json.dumps([{"start": 0.0, "end": 2.0, "text": transcript}])),
        )
    ctx.conn.commit()
    return media_hash


def _run(ctx: StageContext) -> None:
    DaysStage().run(ctx)
    EventStage().run(ctx)
    HomeFilterStage().run(ctx)
    SelectionStage().run(ctx)


def _document(ctx: StageContext, context: TripContext | None = None) -> dict:
    _run(ctx)
    return build_timeline(ctx.conn, ctx.config, context)


class TestSchemaConformance:
    def test_the_schema_itself_is_valid(self, schema: dict) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_a_populated_trip_validates(self, ctx: StageContext, make_media, schema) -> None:
        _seed(ctx, make_media)
        jsonschema.validate(_document(ctx), schema)

    def test_a_trip_with_video_validates(self, ctx: StageContext, make_media, schema) -> None:
        _seed(ctx, make_media)
        _add_video(ctx, make_media, processed=True, transcript="hello from Vienna")
        jsonschema.validate(_document(ctx), schema)

    def test_an_empty_library_validates(self, ctx: StageContext, schema) -> None:
        jsonschema.validate(build_timeline(ctx.conn, ctx.config), schema)

    def test_a_trip_with_context_validates(self, ctx: StageContext, make_media, schema) -> None:
        _seed(ctx, make_media)
        context = TripContext(
            journal_voice="first_person_plural",
            travelers=(Traveler(role="partner", name="A"),),
            notes=("The concert was why we came.",),
        )
        jsonschema.validate(_document(ctx, context), schema)

    def test_media_with_no_gps_validates(self, ctx: StageContext, make_media, schema) -> None:
        _seed(ctx, make_media, 3)
        db.upsert_media(
            ctx.conn,
            make_media("f" * 64, path="/src/NOGPS.jpeg", taken_local=None, taken_utc=None),
        )
        ctx.conn.commit()
        jsonschema.validate(_document(ctx), schema)


class TestAssetIdentity:
    def test_every_asset_is_keyed_by_its_own_id(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media)
        document = _document(ctx)
        assert all(key == value["asset_id"] for key, value in document["assets"].items())

    def test_the_asset_id_prefixes_the_content_hash(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media)
        document = _document(ctx)
        assert all(a["content_hash"].startswith(a["asset_id"]) for a in document["assets"].values())

    def test_ids_survive_a_selection_change(self, ctx: StageContext, make_media) -> None:
        """The reason ids exist: a cell number changes when selection does, a hash does not."""
        _seed(ctx, make_media)
        before = {a["content_hash"]: a["asset_id"] for a in _document(ctx)["assets"].values()}

        narrower = replace(
            ctx,
            config=replace(
                ctx.config, selection=replace(ctx.config.selection, highlights_per_day=2)
            ),
        )
        after = {a["content_hash"]: a["asset_id"] for a in _document(narrower)["assets"].values()}
        assert before == after

    def test_every_referenced_highlight_exists_in_assets(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media)
        document = _document(ctx)
        referenced = {
            asset_id
            for day in document["days"]
            for asset_id in day["highlights"]
            + [a for event in day["events"] for a in event["assets"] + event["highlights"]]
        }
        assert referenced <= set(document["assets"])


class TestVideoRecords:
    def test_a_transcribed_clip_says_so(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=True, transcript="hello")
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        assert video["video"]["transcript_status"] == TranscriptStatus.TRANSCRIBED

    def test_a_processed_silent_clip_is_a_real_negative(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=True, transcript=None)
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        assert video["video"]["transcript_status"] == TranscriptStatus.NO_SPEECH

    def test_an_unprocessed_clip_is_distinguished_from_a_silent_one(
        self, ctx: StageContext, make_media
    ) -> None:
        """The distinction P02 asked for: 'we heard nothing' is not 'we never listened'."""
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=False, transcript=None)
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        assert video["video"]["transcript_status"] == TranscriptStatus.NOT_PROCESSED

    def test_keyframes_reach_the_document(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=True, transcript=None)
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        assert video["video"]["keyframes"] == ["k1.jpg", "k2.jpg"]

    def test_duration_reaches_the_document(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=True, transcript=None)
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        assert video["video"]["duration_seconds"] == 12.5

    def test_a_photo_carries_no_video_block(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        image = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "image")
        assert "video" not in image


class TestQualityComponents:
    def test_components_are_shipped_not_just_overall(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        quality = next(iter(_document(ctx)["assets"].values()))["quality"]
        assert {"sharpness", "exposure", "contrast", "face_count"} <= set(quality)

    def test_no_aesthetic_score_is_invented(self, ctx: StageContext, make_media) -> None:
        """Explicitly Phase 2. A plausible number here would misrepresent what is known."""
        _seed(ctx, make_media, 3)
        quality = next(iter(_document(ctx)["assets"].values()))["quality"]
        assert not {"aesthetic", "composition", "beauty"} & set(quality)

    def test_an_unscored_item_has_a_null_quality_rather_than_a_missing_key(
        self, ctx: StageContext, make_media
    ) -> None:
        db.upsert_media(ctx.conn, make_media("e" * 64, path="/src/X.jpeg"))
        ctx.conn.commit()
        asset = next(iter(build_timeline(ctx.conn, ctx.config)["assets"].values()))
        assert asset["quality"] is None


class TestEventLocation:
    def test_a_moving_event_gets_a_path(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 8)
        event = _document(ctx)["days"][0]["events"][0]
        assert event["location"]["path"] is not None

    def test_a_stationary_event_gets_no_path(self, ctx: StageContext, make_media) -> None:
        """A scattering of points around one courtyard is noise, not movement."""
        start = datetime(2026, 7, 18, 9)
        for index in range(6):
            at = start + timedelta(minutes=5 * index)
            db.upsert_media(
                ctx.conn,
                make_media(
                    f"{index:064x}",
                    path=f"/src/IMG_{index}.jpeg",
                    taken_local=at.isoformat(),
                    taken_utc=at.isoformat(),
                    lat=VIENNA[0] + 0.00001 * index,
                    lon=VIENNA[1],
                ),
            )
        ctx.conn.commit()
        event = _document(ctx)["days"][0]["events"][0]
        assert event["location"]["path"] is None

    def test_the_centroid_is_not_the_only_thing_reported(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media)
        location = _document(ctx)["days"][0]["events"][0]["location"]
        assert location["first"] != location["last"]

    def test_radius_is_reported(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media)
        assert _document(ctx)["days"][0]["events"][0]["location"]["radius_m"] > 0

    def test_gps_coverage_reports_the_fraction_located(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 4)
        assert _document(ctx)["days"][0]["events"][0]["location"]["gps_coverage"] == 1.0


class TestExplicitNegatives:
    def test_an_unconfigured_home_is_stated_rather_than_implied(
        self, ctx: StageContext, make_media
    ) -> None:
        """Zero exclusions with no home configured is a gap, not a clean result."""
        _seed(ctx, make_media, 3)
        privacy = _document(ctx)["privacy"]
        assert privacy["home_configured"] is False

    def test_a_configured_home_is_stated(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        at_home = replace(ctx, config=replace(ctx.config, home=HomeLocation(*VIENNA)))
        assert _document(at_home)["privacy"]["home_configured"] is True

    def test_excluded_media_is_counted(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        at_home = replace(ctx, config=replace(ctx.config, home=HomeLocation(*VIENNA)))
        assert _document(at_home)["privacy"]["excluded_near_home"] == 3

    def test_absent_context_is_flagged_not_just_empty(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        assert _document(ctx)["context"]["supplied"] is False

    def test_supplied_context_is_flagged(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        context = TripContext(notes=("It rained the whole time.",))
        assert _document(ctx, context)["context"]["supplied"] is True

    def test_supplied_notes_reach_the_document(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        context = TripContext(notes=("It rained the whole time.",))
        assert _document(ctx, context)["context"]["notes"] == ["It rained the whole time."]


class TestTimelineStage:
    def test_it_writes_trip_json(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media)
        _run(ctx)
        TimelineStage().run(ctx)
        assert (ctx.out_dir / TRIP_JSON_FILENAME).exists()

    def test_the_written_file_is_valid_json(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media)
        _run(ctx)
        TimelineStage().run(ctx)
        assert json.loads((ctx.out_dir / TRIP_JSON_FILENAME).read_text())["schema_version"] == (
            TRIP_JSON_SCHEMA_VERSION
        )

    def test_the_written_file_validates(self, ctx: StageContext, make_media, schema) -> None:
        _seed(ctx, make_media)
        _run(ctx)
        TimelineStage().run(ctx)
        jsonschema.validate(json.loads((ctx.out_dir / TRIP_JSON_FILENAME).read_text()), schema)

    def test_rerunning_produces_an_identical_document(self, ctx: StageContext, make_media) -> None:
        """A second build must not churn the artifact -- the report diffs against it."""
        _seed(ctx, make_media)
        _run(ctx)
        TimelineStage().run(ctx)
        first = (ctx.out_dir / TRIP_JSON_FILENAME).read_text()

        _run(ctx)
        TimelineStage().run(ctx)
        assert (ctx.out_dir / TRIP_JSON_FILENAME).read_text() == first

    def test_it_survives_an_empty_library(self, ctx: StageContext) -> None:
        TimelineStage().run(ctx)
        assert (ctx.out_dir / TRIP_JSON_FILENAME).exists()
