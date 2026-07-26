"""Unit tests for reverse geocoding: no DB, no filesystem, no network.

`reverse_geocoder.RGeocoder` loads a bundled dataset and builds a k-d tree, so every test here
supplies a tiny in-memory stand-in via `_FakeGeocoder` rather than a real one -- see
`get_offline_geocoder` in `story_book.pipeline.geocode`, which is the only place the real class
is constructed. Nominatim tests never perform a real request: `NominatimClient._call` is always
mocked.
"""

from __future__ import annotations

import pytest

from story_book.config import GeocodeConfig
from story_book.pipeline.geocode import (
    NominatimClient,
    PlaceCandidate,
    available,
    candidate_places,
    reverse_geocode_offline,
)

VIENNA = (48.2082, 16.3738)


class _FakeTree:
    """Mimics `scipy.spatial.cKDTree.query`'s shape quirks: a flat return for `k=1`, a nested
    one for `k>1`, both indexed by a single query point."""

    def __init__(self, ordered_indices: list[int]):
        self._ordered_indices = ordered_indices

    def query(self, points, k):
        assert len(points) == 1
        chosen = self._ordered_indices[:k]
        if k == 1:
            return [0.0], [chosen[0]]
        return [[0.0] * len(chosen)], [chosen]


class _FakeGeocoder:
    def __init__(self, locations: list[dict], ordered_indices: list[int]):
        self.locations = locations
        self.tree = _FakeTree(ordered_indices)


def _make_geocoder() -> _FakeGeocoder:
    locations = [
        {
            "name": "Vienna",
            "admin1": "Vienna",
            "admin2": "Wien Stadt",
            "cc": "AT",
            "lat": "48.2085",
            "lon": "16.3721",
        },
        {
            "name": "Vosendorf",
            "admin1": "Lower Austria",
            "admin2": "",
            "cc": "AT",
            "lat": "48.1211",
            "lon": "16.3404",
        },
        {
            "name": "Bratislava",
            "admin1": "Bratislava",
            "admin2": "",
            "cc": "SK",
            "lat": "48.1486",
            "lon": "17.1077",
        },
    ]
    # Index 0 (Vienna) is nearest, then 1 (Vosendorf), then 2 (Bratislava).
    return _FakeGeocoder(locations, ordered_indices=[0, 1, 2])


class TestAvailable:
    """`available()` gates the stage so a missing `geo` extra degrades rather than aborts."""

    def test_true_when_import_succeeds(self, mocker) -> None:
        mocker.patch.dict("sys.modules", {"reverse_geocoder": mocker.MagicMock()})

        ok, reason = available()

        assert ok is True
        assert reason == ""

    def test_false_when_import_fails(self, mocker) -> None:
        mocker.patch.dict("sys.modules", {"reverse_geocoder": None})

        ok, reason = available()

        assert ok is False
        assert "geo" in reason


class TestCandidatePlaces:
    """The public shortlist API T41 depends on."""

    def test_nearest_first(self) -> None:
        geocoder = _make_geocoder()

        candidates = candidate_places(*VIENNA, count=3, geocoder=geocoder)

        assert [c.name for c in candidates] == ["Vienna", "Vosendorf", "Bratislava"]

    def test_distance_is_a_real_metres_figure(self) -> None:
        geocoder = _make_geocoder()

        [nearest] = candidate_places(*VIENNA, count=1, geocoder=geocoder)

        # Vienna centroid (48.2085, 16.3721) is a few hundred metres from (48.2082, 16.3738).
        assert 0 < nearest.distance_m < 2_000

    def test_respects_requested_count(self) -> None:
        geocoder = _make_geocoder()

        candidates = candidate_places(*VIENNA, count=2, geocoder=geocoder)

        assert len(candidates) == 2

    def test_candidate_carries_admin_and_country_fields(self) -> None:
        geocoder = _make_geocoder()

        [nearest] = candidate_places(*VIENNA, count=1, geocoder=geocoder)

        assert nearest == PlaceCandidate(
            name="Vienna",
            admin1="Vienna",
            admin2="Wien Stadt",
            country_code="AT",
            lat=48.2085,
            lon=16.3721,
            distance_m=nearest.distance_m,
        )

    def test_empty_admin2_is_normalized_to_none(self) -> None:
        geocoder = _make_geocoder()

        candidates = candidate_places(*VIENNA, count=2, geocoder=geocoder)

        assert candidates[1].admin2 is None


class TestReverseGeocodeOffline:
    """The city-level resolution the acceptance criterion requires."""

    def test_wraps_candidate_places_with_count_one(self) -> None:
        geocoder = _make_geocoder()

        result = reverse_geocode_offline(*VIENNA, geocoder=geocoder)

        assert result.name == "Vienna"
        assert result.country_code == "AT"


class TestNominatimClient:
    """Nominatim is optional, rate-limited, and never required."""

    def test_reverse_returns_poi_name_from_call(self, mocker) -> None:
        client = NominatimClient(GeocodeConfig())
        mocker.patch.object(client, "_call", return_value={"name": "Hofburg"})

        assert client.reverse(48.2082, 16.3738) == "Hofburg"

    def test_reverse_returns_none_on_missing_name(self, mocker) -> None:
        client = NominatimClient(GeocodeConfig())
        mocker.patch.object(client, "_call", return_value={})

        assert client.reverse(48.2082, 16.3738) is None

    def test_reverse_degrades_gracefully_on_network_error(self, mocker) -> None:
        client = NominatimClient(GeocodeConfig())
        mocker.patch.object(client, "_call", side_effect=OSError("no route to host"))

        assert client.reverse(48.2082, 16.3738) is None

    def test_first_call_never_sleeps(self, mocker) -> None:
        sleep = mocker.Mock()
        client = NominatimClient(
            GeocodeConfig(nominatim_min_interval_seconds=5.0),
            sleep=sleep,
            monotonic=mocker.Mock(return_value=100.0),
        )
        mocker.patch.object(client, "_call", return_value={"name": "X"})

        client.reverse(0.0, 0.0)

        sleep.assert_not_called()

    def test_second_call_within_the_window_sleeps_the_remainder(self, mocker) -> None:
        sleep = mocker.Mock()
        clock = mocker.Mock(side_effect=[100.0, 100.4])
        client = NominatimClient(
            GeocodeConfig(nominatim_min_interval_seconds=1.1), sleep=sleep, monotonic=clock
        )
        mocker.patch.object(client, "_call", return_value={"name": "X"})

        client.reverse(0.0, 0.0)
        client.reverse(0.0, 0.0)

        sleep.assert_called_once()
        (waited,) = sleep.call_args.args
        assert waited == pytest.approx(0.7, abs=1e-6)

    def test_second_call_after_the_window_does_not_sleep(self, mocker) -> None:
        sleep = mocker.Mock()
        clock = mocker.Mock(side_effect=[100.0, 105.0])
        client = NominatimClient(
            GeocodeConfig(nominatim_min_interval_seconds=1.1), sleep=sleep, monotonic=clock
        )
        mocker.patch.object(client, "_call", return_value={"name": "X"})

        client.reverse(0.0, 0.0)
        client.reverse(0.0, 0.0)

        sleep.assert_not_called()

    def test_call_never_touches_the_real_network(self, mocker) -> None:
        """`_call` is the only place this class reaches the network; every other test mocks it.
        This test confirms `urlopen` itself is never invoked when `_call` is mocked out."""
        urlopen = mocker.patch("urllib.request.urlopen")
        client = NominatimClient(GeocodeConfig())
        mocker.patch.object(client, "_call", return_value={"name": "X"})

        client.reverse(0.0, 0.0)

        urlopen.assert_not_called()
