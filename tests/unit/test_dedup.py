"""Perceptual hashing and near-duplicate clustering, as pure functions."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from story_book.config import Config, DedupConfig
from story_book.db.models import ClusterKind, Media, MediaKind
from story_book.pipeline.dedup import (
    PHASH_WIDTH,
    _Candidate,
    _complete_linkage,
    classify_pair,
    hamming,
)


def _candidate(index: int, *, bits: int, seconds: float = 0.0, vector=None) -> _Candidate:
    media = Media(hash=f"h{index}", path=f"/{index}.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
    return _Candidate(
        media=media,
        phash=bits,
        vector=np.asarray(vector, dtype=np.float32) if vector is not None else None,
        taken=datetime(2026, 7, 18, 12) + timedelta(seconds=seconds),
    )


def _config(**fields) -> Config:
    return Config(dedup=DedupConfig(**fields))


class TestHamming:
    def test_identical_is_zero(self) -> None:
        assert hamming(0b1011, 0b1011) == 0

    def test_counts_differing_bits(self) -> None:
        assert hamming(0b1010, 0b0001) == 3

    def test_is_symmetric(self) -> None:
        assert hamming(12345, 54321) == hamming(54321, 12345)


class TestPhashWidth:
    def test_it_fits_a_signed_64_bit_column(self) -> None:
        """SQLite INTEGER is signed, so a full 64-bit hash overflows on write."""
        assert (1 << PHASH_WIDTH) - 1 < 2**63

    def test_it_is_the_dc_free_block(self) -> None:
        assert PHASH_WIDTH == 63


class TestClassifyPair:
    def test_identical_hashes_are_exact(self) -> None:
        left, right = _candidate(1, bits=0b1111), _candidate(2, bits=0b1111)
        assert classify_pair(left, right, _config()) is ClusterKind.EXACT

    def test_close_and_simultaneous_is_a_burst(self) -> None:
        left = _candidate(1, bits=0b0000, seconds=0)
        right = _candidate(2, bits=0b0011, seconds=1)
        assert classify_pair(left, right, _config(phash_max_distance=18)) is ClusterKind.BURST

    def test_close_but_much_later_is_similar_not_a_burst(self) -> None:
        """A retake ten minutes on is still a duplicate, just not a burst."""
        left = _candidate(1, bits=0b0000, seconds=0)
        right = _candidate(2, bits=0b0011, seconds=600)
        assert classify_pair(left, right, _config(phash_max_distance=18)) is ClusterKind.SIMILAR

    def test_distant_hashes_do_not_match(self) -> None:
        left, right = _candidate(1, bits=0), _candidate(2, bits=(1 << 40) - 1)
        assert classify_pair(left, right, _config(phash_max_distance=18)) is None

    def test_clip_alone_can_never_match(self) -> None:
        """CLIP confirms a pHash match; it never proposes one. Alone it cannot separate the
        classes at all -- real duplicates scored 0.836-0.956 and distinct pairs 0.838-0.929."""
        left = _Candidate(_candidate(1, bits=0).media, None, np.asarray([1.0, 0.0]), None)
        right = _Candidate(_candidate(2, bits=0).media, None, np.asarray([1.0, 0.0]), None)
        assert classify_pair(left, right, _config()) is None

    def test_clip_vetoes_a_phash_match_on_a_different_subject(self) -> None:
        """The failure this catches: two different composer busts, or two different paintings in
        matching frames -- structurally alike at Hamming 14-18, obviously distinct to a person."""
        left = _candidate(1, bits=0b0000, vector=[1.0, 0.0])
        right = _candidate(2, bits=0b0011, vector=[0.7, 0.714])
        assert classify_pair(left, right, _config(phash_max_distance=18)) is None

    def test_clip_confirms_a_phash_match_on_the_same_subject(self) -> None:
        left = _candidate(1, bits=0b0000, vector=[1.0, 0.0])
        right = _candidate(2, bits=0b0011, vector=[0.99, 0.141])
        assert classify_pair(left, right, _config(phash_max_distance=18)) is not None

    def test_without_embeddings_phash_decides_alone(self) -> None:
        """The clip extra is optional; the stage degrades rather than refusing to cluster."""
        left, right = _candidate(1, bits=0b0000), _candidate(2, bits=0b0011)
        assert classify_pair(left, right, _config(phash_max_distance=18)) is not None

    def test_nothing_to_compare_is_no_match(self) -> None:
        left = _Candidate(_candidate(1, bits=0).media, None, None, None)
        right = _Candidate(_candidate(2, bits=0).media, None, None, None)
        assert classify_pair(left, right, _config()) is None


class TestCompleteLinkageDoesNotChain:
    """The bug real data exposed.

    Single-linkage (union-find) merges A with C whenever A~B and B~C, even if A and C are nothing
    alike. On the real trip that produced an 18-photo "burst" with internal distances up to 40 bits
    and eleven false pairs for every true one. Complete linkage refuses to add a photo unless it
    matches everything already in the group.
    """

    def test_a_chain_does_not_become_one_group(self) -> None:
        # 0 <-> 2 bits <-> 4 bits: the ends are 4 apart, beyond a threshold of 3.
        chain = [
            _candidate(1, bits=0b000000),
            _candidate(2, bits=0b000011),
            _candidate(3, bits=0b001111),
        ]
        groups = _complete_linkage(chain, _config(phash_max_distance=3))
        assert all(len(members) <= 2 for _, members in groups)

    def test_a_genuine_group_still_forms(self) -> None:
        tight = [_candidate(i, bits=0b0000 | i) for i in range(3)]
        groups = _complete_linkage(tight, _config(phash_max_distance=18))
        assert len(groups) == 1 and len(groups[0][1]) == 3

    def test_singletons_are_not_clusters(self) -> None:
        alone = [_candidate(1, bits=0), _candidate(2, bits=(1 << 50) - 1)]
        assert _complete_linkage(alone, _config(phash_max_distance=4)) == []

    def test_the_strongest_relationship_names_the_cluster(self) -> None:
        """A burst that drifts into a merely-similar frame is still a burst."""
        members = [
            _candidate(1, bits=0b0000, seconds=0),
            _candidate(2, bits=0b0000, seconds=1),
        ]
        kind, _ = _complete_linkage(members, _config(phash_max_distance=18))[0]
        assert kind is ClusterKind.EXACT

    def test_two_separate_groups_stay_separate(self) -> None:
        members = [
            _candidate(1, bits=0b0000),
            _candidate(2, bits=0b0001),
            _candidate(3, bits=(1 << 40) - 1),
            _candidate(4, bits=(1 << 40) - 2),
        ]
        groups = _complete_linkage(members, _config(phash_max_distance=4))
        assert len(groups) == 2

    def test_every_pair_inside_a_group_is_within_threshold(self) -> None:
        members = [_candidate(i, bits=(1 << i) - 1) for i in range(8)]
        for _, group in _complete_linkage(members, _config(phash_max_distance=4)):
            for left in group:
                for right in group:
                    assert hamming(left.phash, right.phash) <= 4


class TestFalseMergesAreTheExpensiveError:
    """The plan is explicit that losing a distinct photo costs more than keeping a duplicate."""

    def test_a_tighter_threshold_never_grows_a_group(self) -> None:
        members = [_candidate(i, bits=(1 << (i * 2)) - 1) for i in range(6)]
        loose = sum(len(g) for _, g in _complete_linkage(members, _config(phash_max_distance=20)))
        tight = sum(len(g) for _, g in _complete_linkage(members, _config(phash_max_distance=6)))
        assert tight <= loose

    def test_default_threshold_sits_below_the_noise_floor(self) -> None:
        """Measured on 11,709 real within-event pairs: 19 at <=16, 27 at <=18, then 100 at <=20."""
        assert DedupConfig().phash_max_distance == 18

    def test_the_confirmation_gate_sits_in_the_measured_gap(self) -> None:
        """At the ambiguous Hamming band of 13-16, real duplicates scored 0.931-0.952 and the
        false merges a human flagged scored 0.838 and 0.625."""
        assert 0.838 < DedupConfig().confirm_min_cosine < 0.931
