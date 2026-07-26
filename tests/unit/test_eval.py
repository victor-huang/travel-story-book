from __future__ import annotations

from pathlib import Path

import pytest

from story_book.eval import (
    DuplicateGroupSpec,
    KeeperRecord,
    TruthSet,
    TruthSetError,
    _truth_set_from_dict,
    load_truth_set,
    score_boundaries,
    score_keeper_agreement,
    score_pairwise_clusters,
)


class TestTruthSetIsEmpty:
    def test_empty_truth_set_reports_empty(self) -> None:
        assert TruthSet().is_empty is True

    def test_truth_set_with_only_events_is_not_empty(self) -> None:
        assert TruthSet(events=[["a.jpg", "b.jpg"]]).is_empty is False

    def test_truth_set_with_only_duplicate_groups_is_not_empty(self) -> None:
        truth = TruthSet(duplicate_groups=[DuplicateGroupSpec(members=["a.jpg", "b.jpg"])])
        assert truth.is_empty is False


class TestLoadTruthSet:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TruthSetError, match="not found"):
            load_truth_set(tmp_path / "nope.toml")

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "truth.toml"
        path.write_text("this is not [valid toml", encoding="utf-8")
        with pytest.raises(TruthSetError, match="malformed TOML"):
            load_truth_set(path)

    def test_loads_the_example_fixture(self) -> None:
        fixture = Path(__file__).parent.parent / "fixtures" / "truth_set_example.toml"
        truth = load_truth_set(fixture)
        assert truth.trip_name == "Toy trip"
        assert len(truth.events) == 3
        assert len(truth.duplicate_groups) == 3
        assert truth.distinct_pairs == [("IMG_0003.jpg", "IMG_0009.jpg")]
        assert truth.hashes["IMG_0001.jpg"] == "example-hash-0001"

    def test_partial_file_with_only_events_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "truth.toml"
        path.write_text(
            '[[event]]\nitems = ["a.jpg", "b.jpg"]\n',
            encoding="utf-8",
        )
        truth = load_truth_set(path)
        assert truth.events == [["a.jpg", "b.jpg"]]
        assert truth.duplicate_groups == []

    def test_empty_file_loads_as_empty_truth_set(self, tmp_path: Path) -> None:
        path = tmp_path / "truth.toml"
        path.write_text("", encoding="utf-8")
        assert load_truth_set(path).is_empty is True


class TestTruthSetFromDict:
    def test_event_without_items_raises(self) -> None:
        with pytest.raises(TruthSetError, match="items"):
            _truth_set_from_dict({"event": [{"items": []}]})

    def test_duplicate_group_with_single_member_raises(self) -> None:
        with pytest.raises(TruthSetError, match="members"):
            _truth_set_from_dict({"duplicate_group": [{"members": ["a.jpg"]}]})

    def test_duplicate_group_keep_not_in_members_raises(self) -> None:
        raw = {"duplicate_group": [{"members": ["a.jpg", "b.jpg"], "keep": "c.jpg"}]}
        with pytest.raises(TruthSetError, match="keep"):
            _truth_set_from_dict(raw)

    def test_duplicate_group_keep_in_members_is_accepted(self) -> None:
        raw = {"duplicate_group": [{"members": ["a.jpg", "b.jpg"], "keep": "b.jpg"}]}
        truth = _truth_set_from_dict(raw)
        assert truth.duplicate_groups[0].keep == "b.jpg"

    def test_distinct_pair_wrong_length_raises(self) -> None:
        with pytest.raises(TruthSetError, match="distinct_pair"):
            _truth_set_from_dict({"distinct_pair": [{"pair": ["a.jpg"]}]})

    def test_distinct_pair_parses(self) -> None:
        truth = _truth_set_from_dict({"distinct_pair": [{"pair": ["a.jpg", "b.jpg"]}]})
        assert truth.distinct_pairs == [("a.jpg", "b.jpg")]

    def test_trip_section_is_optional(self) -> None:
        truth = _truth_set_from_dict({})
        assert truth.trip_name is None
        assert truth.notes is None


class TestScoreBoundaries:
    def test_too_few_items_is_not_computed(self) -> None:
        score = score_boundaries(["a"], ["a"])
        assert score.computed is False

    def test_perfect_match_scores_perfectly(self) -> None:
        true_groups = [0, 0, 1, 1, 2]
        pred_groups = ["x", "x", "y", "y", "z"]
        score = score_boundaries(true_groups, pred_groups)
        assert score.precision == 1.0
        assert score.recall == 1.0
        assert score.f1 == 1.0
        assert score.target_met is True

    def test_total_mismatch_scores_zero(self) -> None:
        # True boundaries at positions 0 and 2; predicted boundary only at position 1 -- no
        # overlap at all, and both sides have boundaries to get wrong (unlike the vacuous
        # over-split/merge cases below).
        true_groups = [0, 1, 1, 0]
        pred_groups = ["a", "a", "b", "b"]
        score = score_boundaries(true_groups, pred_groups)
        assert score.precision == 0.0
        assert score.recall == 0.0
        assert score.target_met is False

    def test_over_splitting_hurts_precision_not_recall(self) -> None:
        # True: one event of 4 items (no boundaries). Pipeline splits it into 4 events.
        true_groups = [0, 0, 0, 0]
        pred_groups = ["a", "b", "c", "d"]
        score = score_boundaries(true_groups, pred_groups)
        assert score.recall == 1.0  # vacuously -- there were no true boundaries to find
        assert score.precision == 0.0  # every predicted boundary is wrong

    def test_merging_hurts_recall_not_precision(self) -> None:
        # True: 4 distinct events. Pipeline merges them all into one (no predicted boundaries).
        true_groups = [0, 1, 2, 3]
        pred_groups = ["a", "a", "a", "a"]
        score = score_boundaries(true_groups, pred_groups)
        assert score.precision == 1.0  # vacuously -- no predicted boundaries were made
        assert score.recall == 0.0  # every true boundary was missed
        assert score.true_boundaries == 3
        assert score.predicted_boundaries == 0

    def test_mismatched_lengths_raises(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            score_boundaries([1, 2], [1])


class TestScorePairwiseClusters:
    def test_too_few_items_is_not_computed(self) -> None:
        score = score_pairwise_clusters({"a": 0}, {"a": 0})
        assert score.computed is False

    def test_mismatched_keys_raises(self) -> None:
        with pytest.raises(ValueError, match="same keys"):
            score_pairwise_clusters({"a": 0, "b": 0}, {"a": 0})

    def test_perfect_clustering_scores_perfectly(self) -> None:
        true_groups = {"a": 0, "b": 0, "c": 1, "d": 1}
        pred_groups = {"a": "x", "b": "x", "c": "y", "d": "y"}
        score = score_pairwise_clusters(true_groups, pred_groups)
        assert score.precision == 1.0
        assert score.recall == 1.0

    def test_false_merge_of_distinct_items_hurts_precision(self) -> None:
        # a and b are true duplicates; c is truly distinct from both, but the pipeline merges
        # all three into one cluster.
        true_groups = {"a": 0, "b": 0, "c": 1}
        pred_groups = {"a": "x", "b": "x", "c": "x"}
        score = score_pairwise_clusters(true_groups, pred_groups)
        assert score.recall == 1.0  # the true (a, b) duplicate pair was found
        assert score.precision < 1.0  # but (a, c) and (b, c) are false merges

    def test_missed_duplicate_hurts_recall_not_precision(self) -> None:
        # a and b are true duplicates but the pipeline splits them into separate clusters.
        true_groups = {"a": 0, "b": 0}
        pred_groups = {"a": "x", "b": "y"}
        score = score_pairwise_clusters(true_groups, pred_groups)
        assert score.recall == 0.0
        assert score.precision == 1.0  # vacuously -- no predicted-positive pairs exist


class TestScoreKeeperAgreement:
    def test_no_resolvable_records_is_not_computed(self) -> None:
        records = [KeeperRecord(0, "a.jpg", None, resolvable=False)]
        score = score_keeper_agreement(records)
        assert score.computed is False

    def test_perfect_agreement(self) -> None:
        records = [
            KeeperRecord(0, "a.jpg", "a.jpg", resolvable=True),
            KeeperRecord(1, "b.jpg", "b.jpg", resolvable=True),
        ]
        score = score_keeper_agreement(records)
        assert score.agreement == 1.0
        assert score.target_met is True

    def test_zero_agreement(self) -> None:
        records = [
            KeeperRecord(0, "a.jpg", "z.jpg", resolvable=True),
            KeeperRecord(1, "b.jpg", "y.jpg", resolvable=True),
        ]
        score = score_keeper_agreement(records)
        assert score.agreement == 0.0
        assert score.target_met is False

    def test_unresolved_records_are_excluded_from_denominator(self) -> None:
        records = [
            KeeperRecord(0, "a.jpg", "a.jpg", resolvable=True),
            KeeperRecord(1, "b.jpg", None, resolvable=False),
        ]
        score = score_keeper_agreement(records)
        assert score.total == 1
        assert score.unresolved == 1
        assert score.agreement == 1.0
