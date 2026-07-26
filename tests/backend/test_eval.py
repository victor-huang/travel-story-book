from __future__ import annotations

import sqlite3
from pathlib import Path

from story_book.db import connection as db
from story_book.eval import (
    DuplicateGroupSpec,
    TruthSet,
    evaluate,
    evaluate_truth_set_file,
    load_truth_set,
    render_report,
    resolve_truth_set,
)

EXAMPLE_TRUTH_SET = Path(__file__).parent.parent / "fixtures" / "truth_set_example.toml"


def _insert_day(conn: sqlite3.Connection, local_date: str = "2026-07-18") -> int:
    cursor = conn.execute("INSERT INTO day (trip_id, local_date) VALUES (1, ?)", (local_date,))
    return cursor.lastrowid


def _insert_event(conn: sqlite3.Connection, day_id: int, seq: int) -> int:
    cursor = conn.execute("INSERT INTO event (day_id, seq) VALUES (?, ?)", (day_id, seq))
    return cursor.lastrowid


def _link_media_event(conn: sqlite3.Connection, media_hash: str, event_id: int) -> None:
    conn.execute(
        "INSERT INTO media_event (media_hash, event_id) VALUES (?, ?)", (media_hash, event_id)
    )


def _insert_cluster(
    conn: sqlite3.Connection, event_id: int, keeper_hash: str | None = None, kind: str = "similar"
) -> int:
    cursor = conn.execute(
        "INSERT INTO cluster (event_id, kind, keeper_hash) VALUES (?, ?, ?)",
        (event_id, kind, keeper_hash),
    )
    return cursor.lastrowid


def _link_media_cluster(conn: sqlite3.Connection, media_hash: str, cluster_id: int) -> None:
    conn.execute(
        "INSERT INTO media_cluster (media_hash, cluster_id) VALUES (?, ?)",
        (media_hash, cluster_id),
    )


class TestResolveTruthSet:
    def test_matches_filenames_to_media_hashes(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/IMG_0001.jpg"))
        truth = TruthSet(events=[["IMG_0001.jpg"]])

        resolution = resolve_truth_set(conn, truth)

        assert resolution.hash_by_filename["IMG_0001.jpg"] == "h1"
        assert resolution.unmatched == []

    def test_reports_filenames_missing_from_the_db(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/IMG_0001.jpg"))
        truth = TruthSet(events=[["IMG_9999.jpg"]])

        resolution = resolve_truth_set(conn, truth)

        assert resolution.unmatched == ["IMG_9999.jpg"]

    def test_ambiguous_filename_uses_hash_hint(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/one/IMG_0001.jpg"))
        db.upsert_media(conn, make_media("h2", path="/two/IMG_0001.jpg"))
        truth = TruthSet(events=[["IMG_0001.jpg"]], hashes={"IMG_0001.jpg": "h2"})

        resolution = resolve_truth_set(conn, truth)

        assert resolution.hash_by_filename["IMG_0001.jpg"] == "h2"
        assert resolution.ambiguous == []

    def test_ambiguous_filename_without_hint_is_flagged(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/one/IMG_0001.jpg"))
        db.upsert_media(conn, make_media("h2", path="/two/IMG_0001.jpg"))
        truth = TruthSet(events=[["IMG_0001.jpg"]])

        resolution = resolve_truth_set(conn, truth)

        assert resolution.ambiguous == ["IMG_0001.jpg"]


class TestEvaluateEvents:
    def test_not_computed_when_no_events_exist_yet(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        truth = TruthSet(events=[["a.jpg"], ["b.jpg"]])

        report = evaluate(conn, truth)

        assert report.events.computed is False
        assert "not produced any events" in report.events.note

    def test_perfect_event_split_scores_perfectly(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        db.upsert_media(conn, make_media("h3", path="/src/c.jpg", taken_utc="2026-07-18T12:00:00"))
        db.upsert_media(conn, make_media("h4", path="/src/d.jpg", taken_utc="2026-07-18T12:05:00"))
        day = _insert_day(conn)
        event1 = _insert_event(conn, day, 1)
        event2 = _insert_event(conn, day, 2)
        _link_media_event(conn, "h1", event1)
        _link_media_event(conn, "h2", event1)
        _link_media_event(conn, "h3", event2)
        _link_media_event(conn, "h4", event2)
        truth = TruthSet(events=[["a.jpg", "b.jpg"], ["c.jpg", "d.jpg"]])

        report = evaluate(conn, truth)

        assert report.events.computed is True
        assert report.events.precision == 1.0
        assert report.events.recall == 1.0
        assert report.events.target_met is True

    def test_merged_events_reduce_recall(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        db.upsert_media(conn, make_media("h3", path="/src/c.jpg", taken_utc="2026-07-18T12:00:00"))
        day = _insert_day(conn)
        event1 = _insert_event(conn, day, 1)
        # Pipeline merges all three into a single event, but truth says a/b are one event and
        # c is a second -- the merge should cost exactly one missed boundary.
        _link_media_event(conn, "h1", event1)
        _link_media_event(conn, "h2", event1)
        _link_media_event(conn, "h3", event1)
        truth = TruthSet(events=[["a.jpg", "b.jpg"], ["c.jpg"]])

        report = evaluate(conn, truth)

        assert report.events.recall == 0.0
        assert report.events.true_boundaries == 1
        assert report.events.predicted_boundaries == 0


class TestEvaluateDuplicates:
    def test_not_computed_when_no_clusters_exist_yet(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg"))
        truth = TruthSet(duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"])])

        report = evaluate(conn, truth)

        assert report.duplicates.computed is False
        assert "not produced any clusters" in report.duplicates.note

    def test_perfect_clustering_scores_perfectly(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        db.upsert_media(conn, make_media("h3", path="/src/c.jpg", taken_utc="2026-07-18T12:00:00"))
        day = _insert_day(conn)
        event = _insert_event(conn, day, 1)
        cluster_ab = _insert_cluster(conn, event)
        cluster_c = _insert_cluster(conn, event)
        _link_media_cluster(conn, "h1", cluster_ab)
        _link_media_cluster(conn, "h2", cluster_ab)
        _link_media_cluster(conn, "h3", cluster_c)
        truth = TruthSet(duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"])])

        report = evaluate(conn, truth)

        assert report.duplicates.computed is True
        assert report.duplicates.precision == 1.0
        assert report.duplicates.recall == 1.0

    def test_false_merge_reduces_precision(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        db.upsert_media(conn, make_media("h3", path="/src/c.jpg", taken_utc="2026-07-18T12:00:00"))
        day = _insert_day(conn)
        event = _insert_event(conn, day, 1)
        one_big_cluster = _insert_cluster(conn, event)
        _link_media_cluster(conn, "h1", one_big_cluster)
        _link_media_cluster(conn, "h2", one_big_cluster)
        _link_media_cluster(conn, "h3", one_big_cluster)
        # Truth: a/b are duplicates, c is explicitly distinct from a.
        truth = TruthSet(
            duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"])],
            distinct_pairs=[("a.jpg", "c.jpg")],
        )

        report = evaluate(conn, truth)

        assert report.duplicates.recall == 1.0
        assert report.duplicates.precision < 1.0


class TestEvaluateKeeperAgreement:
    def test_not_computed_without_a_keep_label(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg"))
        truth = TruthSet(duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"])])

        report = evaluate(conn, truth)

        assert report.keeper_agreement.computed is False

    def test_matching_keeper_is_full_agreement(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        day = _insert_day(conn)
        event = _insert_event(conn, day, 1)
        cluster = _insert_cluster(conn, event, keeper_hash="h2")
        _link_media_cluster(conn, "h1", cluster)
        _link_media_cluster(conn, "h2", cluster)
        truth = TruthSet(
            duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"], keep="b.jpg")]
        )

        report = evaluate(conn, truth)

        assert report.keeper_agreement.computed is True
        assert report.keeper_agreement.agreement == 1.0
        assert report.keeper_agreement.target_met is True

    def test_mismatched_keeper_is_zero_agreement(self, conn, make_media) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        day = _insert_day(conn)
        event = _insert_event(conn, day, 1)
        cluster = _insert_cluster(conn, event, keeper_hash="h1")
        _link_media_cluster(conn, "h1", cluster)
        _link_media_cluster(conn, "h2", cluster)
        truth = TruthSet(
            duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"], keep="b.jpg")]
        )

        report = evaluate(conn, truth)

        assert report.keeper_agreement.agreement == 0.0
        assert report.keeper_agreement.target_met is False


class TestEvaluateGracefulDegradation:
    def test_empty_truth_set_against_empty_db_reports_not_computed(self, conn) -> None:
        report = evaluate(conn, TruthSet())

        assert report.events.computed is False
        assert report.duplicates.computed is False
        assert report.keeper_agreement.computed is False
        assert report.unmatched_filenames == []

    def test_truth_set_referencing_files_not_in_db_does_not_crash(self, conn) -> None:
        truth = TruthSet(events=[["ghost.jpg", "phantom.jpg"]])

        report = evaluate(conn, truth)

        assert report.events.computed is False
        assert set(report.unmatched_filenames) == {"ghost.jpg", "phantom.jpg"}

    def test_partially_labelled_file_scores_only_its_labelled_sections(
        self, conn, make_media
    ) -> None:
        db.upsert_media(conn, make_media("h1", path="/src/a.jpg", taken_utc="2026-07-18T10:00:00"))
        db.upsert_media(conn, make_media("h2", path="/src/b.jpg", taken_utc="2026-07-18T10:05:00"))
        day = _insert_day(conn)
        event = _insert_event(conn, day, 1)
        _link_media_event(conn, "h1", event)
        _link_media_event(conn, "h2", event)
        truth = TruthSet(events=[["a.jpg", "b.jpg"]])  # no duplicate labels at all

        report = evaluate(conn, truth)

        assert report.events.computed is True
        assert report.duplicates.computed is False
        assert report.keeper_agreement.computed is False

    def test_render_report_never_crashes_on_an_all_uncomputed_report(self, conn) -> None:
        report = evaluate(conn, TruthSet())

        text = render_report(report)

        assert "not yet computed" in text


class TestAcceptanceCriterion:
    """ "Runs against a hand-written toy truth set and reports the metrics named in the plan's
    success criteria." -- exercised here against tests/fixtures/truth_set_example.toml with a
    small synthetic DB standing in for a real pipeline run."""

    def _populate_matching_pipeline_output(self, conn: sqlite3.Connection, make_media) -> None:
        filenames = [f"IMG_000{i}.jpg" for i in range(1, 10)]
        for i, filename in enumerate(filenames, start=1):
            db.upsert_media(
                conn,
                make_media(f"h{i}", path=f"/src/{filename}", taken_utc=f"2026-07-18T10:0{i}:00"),
            )
        day = _insert_day(conn)
        event1, event2, event3 = (_insert_event(conn, day, seq) for seq in (1, 2, 3))
        for hash_ in ("h1", "h2", "h3"):
            _link_media_event(conn, hash_, event1)
        for hash_ in ("h4", "h5"):
            _link_media_event(conn, hash_, event2)
        for hash_ in ("h6", "h7", "h8", "h9"):
            _link_media_event(conn, hash_, event3)

        cluster_12 = _insert_cluster(conn, event1, keeper_hash="h2")
        _link_media_cluster(conn, "h1", cluster_12)
        _link_media_cluster(conn, "h2", cluster_12)

        cluster_45 = _insert_cluster(conn, event2)
        _link_media_cluster(conn, "h4", cluster_45)
        _link_media_cluster(conn, "h5", cluster_45)

        cluster_678 = _insert_cluster(conn, event3, keeper_hash="h7")
        for hash_ in ("h6", "h7", "h8"):
            _link_media_cluster(conn, hash_, cluster_678)

    def test_loads_and_scores_the_example_truth_set(self, conn, make_media) -> None:
        self._populate_matching_pipeline_output(conn, make_media)

        report = evaluate_truth_set_file(conn, EXAMPLE_TRUTH_SET)

        assert report.events.computed is True
        assert report.events.precision >= 0.80
        assert report.events.recall >= 0.80
        assert report.duplicates.computed is True
        assert report.duplicates.precision == 1.0
        assert report.duplicates.recall == 1.0
        assert report.keeper_agreement.computed is True
        assert report.keeper_agreement.agreement == 1.0
        assert report.keeper_agreement.target_met is True

    def test_report_renders_all_three_named_metrics(self, conn, make_media) -> None:
        self._populate_matching_pipeline_output(conn, make_media)
        report = evaluate_truth_set_file(conn, EXAMPLE_TRUTH_SET)

        text = render_report(report)

        assert "Event boundaries" in text
        assert "Duplicate groups" in text
        assert "Keeper agreement" in text

    def test_load_truth_set_matches_evaluate_truth_set_file(self, conn, make_media) -> None:
        self._populate_matching_pipeline_output(conn, make_media)
        truth = load_truth_set(EXAMPLE_TRUTH_SET)

        direct = evaluate(conn, truth, truth_set_path=EXAMPLE_TRUTH_SET)
        via_file = evaluate_truth_set_file(conn, EXAMPLE_TRUTH_SET)

        assert direct.events.precision == via_file.events.precision
        assert direct.duplicates.precision == via_file.duplicates.precision
