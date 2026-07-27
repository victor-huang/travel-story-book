"""Pure timeline helpers: path simplification and asset ids. No DB."""

from __future__ import annotations

from story_book.config import TimelineConfig
from story_book.pipeline.timeline import build_asset_ids, simplify_path

# ~111 m per 0.001 degree of latitude in Vienna, which makes these distances easy to reason about.
VIENNA_LAT = 48.2082
VIENNA_LON = 16.3738


class TestSimplifyPath:
    def test_a_single_point_survives(self):
        assert simplify_path([(VIENNA_LAT, VIENNA_LON)], 25.0) == [(VIENNA_LAT, VIENNA_LON)]

    def test_two_points_survive(self):
        points = [(VIENNA_LAT, VIENNA_LON), (VIENNA_LAT + 0.001, VIENNA_LON)]
        assert simplify_path(points, 25.0) == points

    def test_a_straight_line_collapses_to_its_endpoints(self):
        points = [(VIENNA_LAT + 0.0001 * i, VIENNA_LON) for i in range(20)]
        assert simplify_path(points, 25.0) == [points[0], points[-1]]

    def test_a_corner_is_kept(self):
        points = [
            (VIENNA_LAT, VIENNA_LON),
            (VIENNA_LAT + 0.002, VIENNA_LON),
            (VIENNA_LAT + 0.002, VIENNA_LON + 0.002),
        ]
        assert len(simplify_path(points, 25.0)) == 3

    def test_jitter_around_one_spot_collapses(self):
        """121 photos taken standing still must not become a 121-point path."""
        points = [
            (VIENNA_LAT + 0.00001 * (i % 3), VIENNA_LON + 0.00001 * (i % 2)) for i in range(121)
        ]
        assert len(simplify_path(points, 25.0)) == 2

    def test_a_looser_tolerance_keeps_fewer_points(self):
        points = [
            (VIENNA_LAT, VIENNA_LON),
            (VIENNA_LAT + 0.0005, VIENNA_LON + 0.0002),
            (VIENNA_LAT + 0.001, VIENNA_LON),
        ]
        assert len(simplify_path(points, 5.0)) > len(simplify_path(points, 200.0))


class TestBuildAssetIds:
    def test_ids_are_the_configured_length(self):
        ids = build_asset_ids(["a" * 64, "b" * 64], TimelineConfig(asset_id_length=8))
        assert all(len(value) == 8 for value in ids.values())

    def test_every_hash_gets_an_id(self):
        hashes = [f"{i:064x}" for i in range(50)]
        assert len(build_asset_ids(hashes, TimelineConfig())) == 50

    def test_ids_are_unique(self):
        hashes = [f"{i:064x}" for i in range(50)]
        ids = build_asset_ids(hashes, TimelineConfig())
        assert len(set(ids.values())) == 50

    def test_a_colliding_prefix_lengthens_rather_than_duplicating(self):
        """Two hashes sharing their first 8 characters must not share an id."""
        hashes = ["abcdefab" + "1" * 56, "abcdefab" + "2" * 56]
        ids = build_asset_ids(hashes, TimelineConfig(asset_id_length=8))
        assert len(set(ids.values())) == 2

    def test_the_id_is_a_prefix_of_the_hash(self):
        media_hash = "0123456789abcdef" * 4
        ids = build_asset_ids([media_hash], TimelineConfig(asset_id_length=8))
        assert media_hash.startswith(ids[media_hash])

    def test_the_same_hash_yields_the_same_id_across_calls(self):
        """The whole point of the id: stable across runs, unlike a positional cell number."""
        hashes = [f"{i:064x}" for i in range(10)]
        first = build_asset_ids(hashes, TimelineConfig())
        second = build_asset_ids(list(reversed(hashes)), TimelineConfig())
        assert first == second

    def test_no_hashes_is_not_an_error(self):
        assert build_asset_ids([], TimelineConfig()) == {}
