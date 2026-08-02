"""Overrides against a real DB: each type must demonstrably change the output.

The acceptance criterion for T32 is exactly that, plus the resume guarantee -- re-running after
an edit must recompute no cached stage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from story_book.config import HomeLocation
from story_book.db import connection as db
from story_book.db.models import SelectionScope
from story_book.overrides import OverrideError, Overrides, resolve
from story_book.pipeline.base import StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.home_filter import HomeFilterStage
from story_book.pipeline.selection import SelectionStage

VIENNA = (48.2082, 16.3738)


def _seed(ctx: StageContext, make_media, count: int, *, minutes: float = 5.0) -> None:
    """`count` photos five minutes apart, each with a descending quality score.

    Descending on purpose: it makes "the pipeline would not have chosen this one" easy to state
    -- the last photo is always the worst, so pinning it is a decision no score would reach.
    """
    start = datetime(2026, 7, 18, 9)
    for index in range(count):
        at = start + timedelta(minutes=minutes * index)
        media_hash = f"item{index:03d}"
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
            "content_class) VALUES (?, ?, ?, ?, ?, 'landscape')",
            (media_hash, 0.5, 0.5, 0.5, 0.9 - index * 0.02),
        )
    ctx.conn.commit()


def _with(ctx: StageContext, **raw) -> StageContext:
    return replace(ctx, overrides=Overrides.from_dict({**raw}))


def _run(ctx: StageContext) -> None:
    DaysStage().run(ctx)
    EventStage().run(ctx)
    SelectionStage().run(ctx)


def _day_picks(ctx: StageContext) -> set[str]:
    return {
        row["media_hash"]
        for row in ctx.conn.execute(
            "SELECT media_hash FROM selection WHERE scope = ?", (str(SelectionScope.DAY),)
        )
    }


class TestResolveNames:
    def test_a_bare_stem_matches_the_file(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        resolved = resolve(Overrides.from_dict({"pin": ["IMG_1000"]}), ctx.conn)
        assert resolved.pin == frozenset({"item000"})

    def test_a_full_filename_matches_the_file(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 3)
        resolved = resolve(Overrides.from_dict({"pin": ["IMG_1000.jpeg"]}), ctx.conn)
        assert resolved.pin == frozenset({"item000"})

    def test_an_unknown_name_is_fatal_rather_than_ignored(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 3)
        with pytest.raises(OverrideError, match="no media in this library"):
            resolve(Overrides.from_dict({"pin": ["IMG_9999"]}), ctx.conn)

    def test_an_ambiguous_name_is_fatal(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 2)
        db.upsert_media(ctx.conn, make_media("other", path="/elsewhere/IMG_1000.jpeg"))
        with pytest.raises(OverrideError, match="matches 2 different files"):
            resolve(Overrides.from_dict({"pin": ["IMG_1000"]}), ctx.conn)


class TestPinAndReject:
    def test_the_worst_photo_is_not_chosen_without_a_pin(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 30)
        _run(ctx)
        assert "item029" not in _day_picks(ctx)

    def test_pinning_the_worst_photo_puts_it_in_the_book(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 30)
        _run(_with(ctx, pin=["IMG_1029"]))
        assert "item029" in _day_picks(ctx)

    def test_a_pin_is_additional_to_the_quota_not_deducted_from_it(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 30)
        _run(ctx)
        before = len(_day_picks(ctx))

        _run(_with(ctx, pin=["IMG_1029"]))
        assert len(_day_picks(ctx)) == before + 1

    def test_rejecting_the_best_photo_keeps_it_out(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 30)
        _run(ctx)
        assert "item000" in _day_picks(ctx)

        _run(_with(ctx, reject=["IMG_1000"]))
        assert "item000" not in _day_picks(ctx)

    def test_a_pin_is_recorded_with_its_reason(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 30)
        _run(_with(ctx, pin=["IMG_1029"]))
        reason = ctx.conn.execute(
            "SELECT reason FROM selection WHERE media_hash = 'item029' AND scope = ?",
            (str(SelectionScope.DAY),),
        ).fetchone()["reason"]
        assert reason == "pinned"


class TestPinDoesNotBreakPrivacy:
    def test_a_pinned_photo_near_home_is_still_excluded(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 5)
        at_home = _with(
            replace(ctx, config=replace(ctx.config, home=HomeLocation(*VIENNA))),
            pin=["IMG_1000"],
        )
        DaysStage().run(at_home)
        EventStage().run(at_home)
        HomeFilterStage().run(at_home)
        SelectionStage().run(at_home)

        assert "item000" not in _day_picks(ctx)


class TestForcedKeeper:
    def test_a_named_frame_becomes_its_clusters_keeper(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        event_id = ctx.conn.execute("SELECT id FROM event LIMIT 1").fetchone()["id"]
        ctx.conn.execute(
            "INSERT INTO cluster (id, event_id, kind, keeper_hash) VALUES (1, ?, 'burst', NULL)",
            (event_id,),
        )
        ctx.conn.executemany(
            "INSERT INTO media_cluster (media_hash, cluster_id) VALUES (?, 1)",
            [("item000",), ("item001",)],
        )
        ctx.conn.commit()

        SelectionStage().run(_with(ctx, keeper=["IMG_1001"]))
        keeper = ctx.conn.execute("SELECT keeper_hash FROM cluster WHERE id = 1").fetchone()
        assert keeper["keeper_hash"] == "item001"


class TestEventOverrides:
    def test_split_before_starts_a_new_event(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert len(ctx.conn.execute("SELECT id FROM event").fetchall()) == 1

        EventStage().run(_with(ctx, split_event=[{"before": "IMG_1003"}]))
        assert len(ctx.conn.execute("SELECT id FROM event").fetchall()) == 2

    def test_merge_rejoins_what_a_split_separated(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(
            _with(
                ctx,
                split_event=[{"before": "IMG_1002"}, {"before": "IMG_1004"}],
                merge_events=[{"photos": ["IMG_1002", "IMG_1004"]}],
            )
        )
        assert len(ctx.conn.execute("SELECT id FROM event").fetchall()) == 2

    def test_event_sequence_numbers_stay_contiguous_after_a_merge(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(
            _with(
                ctx,
                split_event=[{"before": "IMG_1002"}, {"before": "IMG_1004"}],
                merge_events=[{"photos": ["IMG_1002", "IMG_1004"]}],
            )
        )
        seqs = [r["seq"] for r in ctx.conn.execute("SELECT seq FROM event ORDER BY seq")]
        assert seqs == [1, 2]

    def test_label_names_the_event_holding_the_photo(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(_with(ctx, label_event=[{"photo": "IMG_1003", "label": "The concert"}]))
        label = ctx.conn.execute("SELECT label FROM event LIMIT 1").fetchone()["label"]
        assert label == "The concert"

    def test_an_unlabelled_event_keeps_a_null_label(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        DaysStage().run(ctx)
        EventStage().run(ctx)
        assert ctx.conn.execute("SELECT label FROM event LIMIT 1").fetchone()["label"] is None


class TestOverridesCostNothingToApply:
    def test_editing_overrides_recomputes_no_cached_stage(
        self, ctx: StageContext, make_media
    ) -> None:
        """The point of the file: correct, re-run, and no expensive stage is invalidated.

        `days`, `events` and `selection` are `always_run` aggregates and re-derive by design;
        what must not happen is a cached per-item result being dropped.
        """
        _seed(ctx, make_media, 10)
        ctx.conn.execute(
            "INSERT INTO stage_result (media_hash, stage, stage_version, status, computed_at) "
            "VALUES ('item000', 'embeddings', 1, 'ok', '2026-07-18T00:00:00')"
        )
        ctx.conn.commit()

        _run(_with(ctx, pin=["IMG_1009"], reject=["IMG_1000"]))

        still_cached = ctx.conn.execute(
            "SELECT status FROM stage_result WHERE media_hash = 'item000' AND stage = 'embeddings'"
        ).fetchone()
        assert still_cached["status"] == "ok"


class TestRejectRemovesFromTheArtifactNotJustTheHighlights:
    """`overrides.toml` says "never include these", and it now means that.

    Found on the real trip: two screen captures were correctly classified `screenshot` and so were
    already out of the highlights — but they still counted toward the day, dropped pins on the map,
    and formed a 00:59 "stop" that was two phone screens. Rejecting only from selection was not
    what the file promised.
    """

    def _doc(self, ctx: StageContext, **raw):
        from story_book.pipeline.timeline import build_timeline

        return build_timeline(ctx.conn, ctx.config, None, ctx.out_dir, Overrides.from_dict(raw))

    def test_a_rejected_item_is_absent_from_trip_json(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        _run(ctx)
        doc = self._doc(ctx, reject=["IMG_1000"])

        assert "IMG_1000.jpeg" not in {a["filename"] for a in doc["assets"].values()}

    def test_it_is_still_in_the_database(self, ctx: StageContext, make_media) -> None:
        """Non-destructive: rejected means not part of the story, never deleted."""
        _seed(ctx, make_media, 6)
        _run(ctx)
        self._doc(ctx, reject=["IMG_1000"])

        assert (
            ctx.conn.execute("SELECT COUNT(*) AS n FROM media WHERE hash = 'item000'").fetchone()[
                "n"
            ]
            == 1
        )

    def test_the_day_count_drops(self, ctx: StageContext, make_media) -> None:
        _seed(ctx, make_media, 6)
        _run(ctx)
        before = self._doc(ctx)["days"][0]["counts"]["media"]
        after = self._doc(ctx, reject=["IMG_1000"])["days"][0]["counts"]["media"]

        assert after == before - 1

    def test_the_exclusion_is_counted_rather_than_silent(
        self, ctx: StageContext, make_media
    ) -> None:
        _seed(ctx, make_media, 6)
        _run(ctx)

        assert self._doc(ctx, reject=["IMG_1000"])["privacy"]["excluded_by_override"] == 1

    def test_an_event_left_with_nothing_disappears(self, ctx: StageContext, make_media) -> None:
        """Two screenshots taken indoors were never a stop on the trip."""
        _seed(ctx, make_media, 6, minutes=5.0)
        db.upsert_media(
            ctx.conn,
            make_media(
                "late000",
                path="/src/IMG_9001.jpeg",
                taken_local="2026-07-18T23:30:00",
                taken_utc="2026-07-18T21:30:00",
                lat=VIENNA[0],
                lon=VIENNA[1],
            ),
        )
        ctx.conn.execute(
            "INSERT INTO score (media_hash, sharpness, exposure, contrast, overall, "
            "content_class) VALUES ('late000', 0.5, 0.5, 0.5, 0.5, 'screenshot')"
        )
        ctx.conn.commit()
        _run(ctx)
        before = len(self._doc(ctx)["days"][0]["events"])

        after = self._doc(ctx, reject=["IMG_9001"])["days"][0]["events"]
        assert len(after) == before - 1

    def test_an_event_that_merely_lost_its_highlight_is_kept(
        self, ctx: StageContext, make_media
    ) -> None:
        """Different from the above: a real stop whose photograph was not selected still happened,
        and the brief lists it on purpose."""
        _seed(ctx, make_media, 6)
        _run(ctx)
        doc = self._doc(ctx, reject=["IMG_1000"])

        assert doc["days"][0]["events"], "the remaining media still forms a stop"
