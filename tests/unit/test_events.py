"""Event detection logic, as pure functions over in-memory media."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from story_book.config import Config, EventConfig, TimeConfig
from story_book.db.models import Media, MediaKind
from story_book.pipeline.events import (
    detect_events,
    haversine_km,
    starts_new_event,
)

VIENNA = (48.2082, 16.3738)


def _media(
    index: int,
    *,
    at: datetime,
    lat: float | None = VIENNA[0],
    lon: float | None = VIENNA[1],
    place_id: int | None = None,
) -> Media:
    return Media(
        hash=f"h{index:04d}",
        path=f"/src/IMG_{index:04d}.jpg",
        kind=MediaKind.IMAGE,
        bytes=1000,
        mtime=0.0,
        taken_local=at.isoformat(),
        taken_utc=at.isoformat(),
        lat=lat,
        lon=lon,
        place_id=place_id,
    )


def _walk(start: datetime, count: int, *, minutes: float = 2.0, drift_km: float = 0.0):
    """A sequence of photos, optionally drifting steadily away from the start."""
    items = []
    for index in range(count):
        # ~111 km per degree of latitude.
        lat = VIENNA[0] + (drift_km * index) / 111.0
        items.append(_media(index, at=start + timedelta(minutes=minutes * index), lat=lat))
    return items


def _config(**events) -> Config:
    return Config(events=EventConfig(**events), time=TimeConfig())


class TestHaversine:
    def test_identical_points_are_zero(self) -> None:
        assert haversine_km(VIENNA, VIENNA) == pytest.approx(0.0)

    def test_one_degree_of_latitude_is_about_111_km(self) -> None:
        assert haversine_km((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111.19, abs=0.5)

    def test_it_is_symmetric(self) -> None:
        istanbul = (41.0082, 28.9784)
        assert haversine_km(VIENNA, istanbul) == pytest.approx(haversine_km(istanbul, VIENNA))


class TestStartsNewEvent:
    def test_an_empty_event_never_splits(self) -> None:
        candidate = _media(1, at=datetime(2026, 7, 18, 9))
        assert starts_new_event([], candidate, _config())[0] is False

    def test_a_long_time_gap_splits(self) -> None:
        current = [_media(1, at=datetime(2026, 7, 18, 9))]
        candidate = _media(2, at=datetime(2026, 7, 18, 11))
        split, reason = starts_new_event(current, candidate, _config(gap_minutes=45))
        assert (split, reason) == (True, "time_gap")

    def test_a_short_gap_does_not_split(self) -> None:
        current = [_media(1, at=datetime(2026, 7, 18, 9))]
        candidate = _media(2, at=datetime(2026, 7, 18, 9, 5))
        assert starts_new_event(current, candidate, _config(gap_minutes=45))[0] is False

    def test_a_distant_position_splits(self) -> None:
        current = [_media(1, at=datetime(2026, 7, 18, 9))]
        candidate = _media(2, at=datetime(2026, 7, 18, 9, 5), lat=48.30, lon=16.3738)
        split, reason = starts_new_event(current, candidate, _config(jump_km=1.5))
        assert (split, reason) == (True, "gps_jump")

    def test_a_nearby_position_does_not_split(self) -> None:
        current = [_media(1, at=datetime(2026, 7, 18, 9))]
        candidate = _media(2, at=datetime(2026, 7, 18, 9, 5), lat=48.2090, lon=16.3740)
        assert starts_new_event(current, candidate, _config(jump_km=1.5))[0] is False

    def test_an_item_without_coordinates_cannot_trigger_a_jump(self) -> None:
        current = [_media(1, at=datetime(2026, 7, 18, 9))]
        candidate = _media(2, at=datetime(2026, 7, 18, 9, 5), lat=None, lon=None)
        assert starts_new_event(current, candidate, _config())[0] is False

    def test_a_jump_is_measured_against_located_items_only(self) -> None:
        """An unlocated item in the window must not drag the centroid or block the check."""
        current = [
            _media(1, at=datetime(2026, 7, 18, 9)),
            _media(2, at=datetime(2026, 7, 18, 9, 1), lat=None, lon=None),
        ]
        candidate = _media(3, at=datetime(2026, 7, 18, 9, 5), lat=48.30, lon=16.3738)
        assert starts_new_event(current, candidate, _config(jump_km=1.5))[0] is True


class TestClustersAreDeliberatelyCoarse:
    """Events are internal scoping, not chapters -- see the module docstring.

    P03 measured that real chapter boundaries fall at 2-minute gaps and 10 metres, invisible to
    any time or location rule. Rather than chase them, this stage produces honest time-and-location
    clusters and leaves semantics to the human and the model. There is deliberately no
    maximum-duration rule: a long cluster is harmless when clusters are internal, and *safer* for
    deduplication, since near-duplicates can only be found within a cluster.
    """

    def test_gradual_drift_is_eventually_noticed(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 60, minutes=2.0, drift_km=0.2)
        assert len(detect_events(items, _config(gap_minutes=999, jump_km=1.5))) > 1

    def test_staying_put_produces_one_event(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 20, minutes=2.0, drift_km=0.0)
        assert len(detect_events(items, _config(gap_minutes=45, jump_km=1.5))) == 1

    def test_a_long_stationary_burst_stays_one_cluster(self) -> None:
        """Nine hours in one spot is one cluster, on purpose -- it keeps duplicates comparable."""
        items = _walk(datetime(2026, 7, 18, 9), 200, minutes=2.0, drift_km=0.0)
        assert len(detect_events(items, _config(gap_minutes=45, jump_km=1.5))) == 1

    def test_a_fast_move_still_splits_on_position(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 20, minutes=2.0, drift_km=1.0)
        assert len(detect_events(items, _config(gap_minutes=999, jump_km=1.5))) > 5

    def test_a_long_pause_still_splits_on_time(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 3, minutes=1.0)
        items += _walk(datetime(2026, 7, 18, 15), 3, minutes=1.0)
        for index, media in enumerate(items):
            media.hash = f"h{index:04d}"
        assert len(detect_events(items, _config(gap_minutes=45, jump_km=1.5))) == 2


class TestDetectEvents:
    def test_every_dated_item_lands_in_exactly_one_event(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 25, minutes=10.0, drift_km=0.3)
        events = detect_events(items, _config())
        placed = [m.hash for event in events for m in event.members]
        assert sorted(placed) == sorted(m.hash for m in items)

    def test_undated_items_are_excluded(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 3)
        items.append(Media(hash="undated", path="/x.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0))
        events = detect_events(items, _config())
        assert not any(m.hash == "undated" for event in events for m in event.members)

    def test_events_never_span_two_days(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 5) + _walk(datetime(2026, 7, 19, 9), 5)
        events = detect_events(items, _config())
        assert all(len({e.local_date for e in [event]}) == 1 for event in events)

    def test_sequence_numbers_restart_each_day(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 3) + _walk(datetime(2026, 7, 19, 9), 3)
        events = detect_events(items, _config())
        assert min(e.seq for e in events) == 1

    def test_sequence_numbers_are_chronological_within_a_day(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 4, minutes=120.0)
        events = detect_events(items, _config(gap_minutes=45))
        starts = [e.start_utc for e in events]
        assert starts == sorted(starts)

    def test_input_order_does_not_matter(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 12, minutes=10.0)
        forward = detect_events(items, _config())
        backward = detect_events(list(reversed(items)), _config())
        assert [len(e.members) for e in forward] == [len(e.members) for e in backward]

    def test_empty_input_produces_no_events(self) -> None:
        assert detect_events([], _config()) == []


class TestEventProperties:
    def test_centroid_averages_located_members(self) -> None:
        items = [
            _media(1, at=datetime(2026, 7, 18, 9), lat=48.0, lon=16.0),
            _media(2, at=datetime(2026, 7, 18, 9, 1), lat=50.0, lon=18.0),
        ]
        # jump_km raised so the two far-apart points stay in one event; the centroid is the
        # subject here, not the split rule.
        event = detect_events(items, _config(jump_km=99999))[0]
        assert event.centroid == (pytest.approx(49.0), pytest.approx(17.0))

    def test_centroid_is_none_without_coordinates(self) -> None:
        items = [_media(1, at=datetime(2026, 7, 18, 9), lat=None, lon=None)]
        assert detect_events(items, _config())[0].centroid == (None, None)

    def test_place_id_is_the_majority_not_the_first(self) -> None:
        """An event that starts while walking can have its first photo resolve elsewhere."""
        items = [
            _media(1, at=datetime(2026, 7, 18, 9), place_id=99),
            _media(2, at=datetime(2026, 7, 18, 9, 1), place_id=7),
            _media(3, at=datetime(2026, 7, 18, 9, 2), place_id=7),
        ]
        assert detect_events(items, _config())[0].place_id == 7

    def test_place_id_is_none_when_nothing_resolved(self) -> None:
        items = [_media(1, at=datetime(2026, 7, 18, 9))]
        assert detect_events(items, _config())[0].place_id is None

    def test_start_and_end_bracket_the_members(self) -> None:
        items = _walk(datetime(2026, 7, 18, 9), 5, minutes=1.0)
        event = detect_events(items, _config())[0]
        assert event.start_utc == items[0].taken_utc and event.end_utc == items[-1].taken_utc
