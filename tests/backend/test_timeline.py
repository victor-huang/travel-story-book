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
                tz_offset_minutes=120,
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
        assert [k["path"] for k in video["video"]["keyframes"]] == ["k1.jpg", "k2.jpg"]

    def test_each_keyframe_carries_its_offset_into_the_clip(
        self, ctx: StageContext, make_media
    ) -> None:
        """A suggested duration says how long to use footage, not which part of it."""
        _seed(ctx, make_media, 3)
        _add_video(ctx, make_media, processed=True, transcript=None)
        video = next(a for a in _document(ctx)["assets"].values() if a["kind"] == "video")
        seconds = [k["seconds"] for k in video["video"]["keyframes"]]
        assert seconds == sorted(seconds) and all(0 <= s <= 12.5 for s in seconds)

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


class TestTimestampsAreSelfDescribing:
    """P05: a bare local timestamp is ambiguous the moment a trip crosses a zone."""

    def test_local_time_carries_its_utc_offset(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        asset = next(iter(_document(ctx)["assets"].values()))
        assert asset["taken_local"].endswith("+02:00")

    def test_the_utc_instant_is_reported_alongside(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        asset = next(iter(_document(ctx)["assets"].values()))
        assert asset["taken_utc"]

    def test_the_iana_zone_and_offset_are_both_reported(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 3)
        zone = next(iter(_document(ctx)["assets"].values()))["timezone"]
        assert zone["name"] == "Europe/Vienna" and zone["offset_minutes"] == 120

    def test_a_missing_offset_leaves_the_bare_local_time(self) -> None:
        from story_book.pipeline.timeline import local_with_offset

        assert local_with_offset("2026-07-18T11:03:22", None) == "2026-07-18T11:03:22"

    def test_a_negative_offset_is_formatted_correctly(self) -> None:
        from story_book.pipeline.timeline import local_with_offset

        assert local_with_offset("2026-07-18T11:03:22", -420).endswith("-07:00")

    def test_the_day_reports_the_zone_it_was_lived_in(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        assert _document(ctx)["days"][0]["timezone"] == "Europe/Vienna"


class TestGeometryForLayout:
    def test_a_wide_frame_is_landscape(self) -> None:
        from story_book.pipeline.timeline import geometry

        assert geometry(4000, 3000)["orientation"] == "landscape"

    def test_a_tall_frame_is_portrait(self) -> None:
        from story_book.pipeline.timeline import geometry

        assert geometry(3000, 4000)["orientation"] == "portrait"

    def test_a_square_frame_is_neither(self) -> None:
        from story_book.pipeline.timeline import geometry

        assert geometry(2000, 2000)["orientation"] == "square"

    def test_the_aspect_ratio_is_reported(self) -> None:
        from story_book.pipeline.timeline import geometry

        assert geometry(4000, 3000)["aspect_ratio"] == 1.3333

    def test_unknown_dimensions_do_not_invent_an_orientation(self) -> None:
        from story_book.pipeline.timeline import geometry

        assert geometry(None, None)["orientation"] is None

    def test_geometry_reaches_the_document(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        asset = next(iter(_document(ctx)["assets"].values()))
        assert asset["geometry"]["orientation"] == "landscape"


class TestContentClassesExcludedFromTheArtifact:
    """Screenshots leave `trip.json` by default, configurably.

    Distinct from `quality.reject_content_classes`, which only removes highlight eligibility. A
    screenshot that is merely ineligible still counts toward the day, drops a pin on the map, and
    can be the only member of a stop -- on a real trip two screen captures invented a 00:59 stop
    that was two phone screens.
    """

    def _classify(self, ctx: StageContext, media_hash: str, content_class: str) -> None:
        ctx.conn.execute(
            "UPDATE score SET content_class = ? WHERE media_hash = ?", (content_class, media_hash)
        )
        ctx.conn.commit()

    def test_a_screenshot_is_absent_by_default(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "screenshot")

        names = {a["filename"] for a in _document(ctx)["assets"].values()}
        assert "IMG_1000.jpeg" not in names

    def test_a_receipt_is_kept(self, ctx: StageContext, make_media) -> None:
        """A receipt is a photograph of something in front of you -- weak evidence, but evidence."""
        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "receipt")

        names = {a["filename"] for a in _document(ctx)["assets"].values()}
        assert "IMG_1000.jpeg" in names

    def test_the_exclusion_is_counted(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "screenshot")

        assert _document(ctx)["privacy"]["excluded_by_content_class"] == 1

    def test_the_active_policy_is_stated_in_the_document(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 3)
        assert _document(ctx)["privacy"]["excluded_content_classes"] == ["screenshot"]

    def test_the_policy_is_configurable(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "receipt")
        strict = replace(
            ctx,
            config=replace(
                ctx.config,
                timeline=replace(
                    ctx.config.timeline, exclude_content_classes=("screenshot", "receipt")
                ),
            ),
        )
        names = {a["filename"] for a in _document(strict)["assets"].values()}
        assert "IMG_1000.jpeg" not in names

    def test_an_empty_policy_keeps_everything(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "screenshot")
        keep_all = replace(
            ctx,
            config=replace(
                ctx.config, timeline=replace(ctx.config.timeline, exclude_content_classes=())
            ),
        )
        names = {a["filename"] for a in _document(keep_all)["assets"].values()}
        assert "IMG_1000.jpeg" in names

    def test_a_pinned_screenshot_survives(self, ctx: StageContext, make_media) -> None:
        """The human's word beats the automatic filter, as it does the quality floor."""
        from story_book.overrides import Overrides
        from story_book.pipeline.timeline import build_timeline

        _seed(ctx, make_media, 5)
        self._classify(ctx, f"{0:064x}", "screenshot")
        DaysStage().run(ctx)
        EventStage().run(ctx)
        SelectionStage().run(ctx)

        doc = build_timeline(
            ctx.conn, ctx.config, None, ctx.out_dir, Overrides.from_dict({"pin": ["IMG_1000"]})
        )
        assert "IMG_1000.jpeg" in {a["filename"] for a in doc["assets"].values()}

    def test_a_stop_made_only_of_screenshots_disappears(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 4)
        db.upsert_media(
            ctx.conn,
            make_media(
                "shot000",
                path="/src/IMG_9002.jpeg",
                taken_local="2026-07-18T23:30:00",
                taken_utc="2026-07-18T21:30:00",
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, overall, "
            "content_class) VALUES ('shot000', 0.5, 0.5, 0.5, 0.5, 'screenshot')"
        )
        ctx.conn.commit()

        events = _document(ctx)["days"][0]["events"]
        assert all(e["counts"]["media"] > 0 for e in events)
        assert not any(str(e["start_local"]).endswith("23:30:00") for e in events)
