"""Unit tests for GPS interpolation maths: no DB, no filesystem, no network.

`Media` lists are built directly via the `make_media` fixture; `backfill_gps` is pure.
"""

from __future__ import annotations

from story_book.config import Config, TimeConfig
from story_book.db.models import GpsSource
from story_book.pipeline.gps_backfill import backfill_gps

SALZBURG = (47.8095, 13.0550)
VIENNA = (48.2082, 16.3738)


def _config(window_minutes: float = 120.0) -> Config:
    return Config(time=TimeConfig(gps_interpolation_window_minutes=window_minutes))


class TestInterpolationBetweenTwoNeighbors:
    def test_location_is_linearly_interpolated_by_time_fraction(self, make_media) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:30:00+00:00")
        after = make_media(
            "after",
            taken_utc="2026-07-18T11:00:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        filled = backfill_gps([before, target, after], _config())

        assert [m.hash for m in filled] == ["target"]
        expected_lat = SALZBURG[0] + 0.5 * (VIENNA[0] - SALZBURG[0])
        expected_lon = SALZBURG[1] + 0.5 * (VIENNA[1] - SALZBURG[1])
        assert target.lat == expected_lat
        assert target.lon == expected_lon
        assert target.gps_source == GpsSource.INTERPOLATED

    def test_confidence_is_set_and_positive_for_a_close_bracket(self, make_media) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:01:00+00:00")
        after = make_media(
            "after",
            taken_utc="2026-07-18T10:02:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        backfill_gps([before, target, after], _config())

        assert target.gps_confidence is not None
        assert target.gps_confidence > 0.9


class TestWindowRefusal:
    def test_refuses_to_interpolate_across_a_gap_larger_than_the_configured_window(
        self, make_media
    ) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T06:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:00:00+00:00")
        after = make_media(
            "after",
            taken_utc="2026-07-18T14:00:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        filled = backfill_gps([before, target, after], _config(window_minutes=120.0))

        assert filled == []
        assert target.gps_source == GpsSource.NONE
        assert target.lat is None
        assert target.lon is None
        assert target.gps_confidence is None


class TestOneSidedNeighbor:
    def test_extrapolates_from_a_single_neighbor_within_the_window(self, make_media) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:10:00+00:00")

        filled = backfill_gps([before, target], _config())

        assert [m.hash for m in filled] == ["target"]
        assert target.lat == SALZBURG[0]
        assert target.lon == SALZBURG[1]
        assert target.gps_source == GpsSource.INTERPOLATED

    def test_refuses_a_single_neighbor_beyond_the_window(self, make_media) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T06:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:00:00+00:00")

        filled = backfill_gps([before, target], _config(window_minutes=120.0))

        assert filled == []
        assert target.gps_source == GpsSource.NONE

    def test_one_sided_confidence_is_penalized_relative_to_two_sided_at_the_same_distance(
        self, make_media
    ) -> None:
        # One-sided case: neighbor 10 minutes away.
        one_sided_before = make_media(
            "before",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        one_sided_target = make_media("target_one", taken_utc="2026-07-18T10:10:00+00:00")
        backfill_gps([one_sided_before, one_sided_target], _config())

        # Two-sided case: neighbors 10 minutes away on each side (same total distance profile).
        two_sided_before = make_media(
            "before2",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        two_sided_target = make_media("target_two", taken_utc="2026-07-18T10:10:00+00:00")
        two_sided_after = make_media(
            "after2",
            taken_utc="2026-07-18T10:20:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )
        backfill_gps([two_sided_before, two_sided_target, two_sided_after], _config())

        assert one_sided_target.gps_confidence < two_sided_target.gps_confidence


class TestExifLocationIsNeverOverwritten:
    def test_item_with_existing_exif_gps_is_left_alone(self, make_media) -> None:
        has_gps = make_media(
            "has_gps",
            taken_utc="2026-07-18T10:05:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        other = make_media(
            "other",
            taken_utc="2026-07-18T10:10:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )

        filled = backfill_gps([has_gps, other], _config())

        assert filled == []
        assert has_gps.lat == SALZBURG[0]
        assert has_gps.lon == SALZBURG[1]
        assert has_gps.gps_source == GpsSource.EXIF


class TestNoGpsBearingItemsAtAll:
    def test_nothing_is_filled_when_no_anchors_exist(self, make_media) -> None:
        first = make_media("first", taken_utc="2026-07-18T10:00:00+00:00")
        second = make_media("second", taken_utc="2026-07-18T10:05:00+00:00")

        filled = backfill_gps([first, second], _config())

        assert filled == []
        assert first.gps_source == GpsSource.NONE
        assert second.gps_source == GpsSource.NONE


class TestUndatedItemsAreLeftAlone:
    def test_item_with_no_taken_utc_is_never_filled(self, make_media) -> None:
        anchor = make_media(
            "anchor",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        undated = make_media("undated", taken_utc=None)

        filled = backfill_gps([anchor, undated], _config())

        assert filled == []
        assert undated.gps_source == GpsSource.NONE


class TestConfidenceDecreasesWithDistance:
    def test_two_sided_confidence_drops_as_the_bracket_widens(self, make_media) -> None:
        def bracket_confidence(gap_minutes: int):
            before = make_media(
                f"before_{gap_minutes}",
                taken_utc="2026-07-18T10:00:00+00:00",
                lat=SALZBURG[0],
                lon=SALZBURG[1],
                gps_source=GpsSource.EXIF,
            )
            target_time = f"2026-07-18T{10 + gap_minutes // 60:02d}:{gap_minutes % 60:02d}:00+00:00"
            target = make_media(f"target_{gap_minutes}", taken_utc=target_time)
            after_minutes = gap_minutes * 2
            after_time = (
                f"2026-07-18T{10 + after_minutes // 60:02d}:{after_minutes % 60:02d}:00+00:00"
            )
            after = make_media(
                f"after_{gap_minutes}",
                taken_utc=after_time,
                lat=VIENNA[0],
                lon=VIENNA[1],
                gps_source=GpsSource.EXIF,
            )
            backfill_gps([before, target, after], _config())
            return target.gps_confidence

        close = bracket_confidence(5)
        far = bracket_confidence(40)

        assert close is not None and far is not None
        assert close > far

    def test_one_sided_confidence_drops_as_the_gap_widens(self, make_media) -> None:
        def gap_confidence(gap_minutes: int):
            before = make_media(
                f"anchor_{gap_minutes}",
                taken_utc="2026-07-18T10:00:00+00:00",
                lat=SALZBURG[0],
                lon=SALZBURG[1],
                gps_source=GpsSource.EXIF,
            )
            target_time = f"2026-07-18T{10 + gap_minutes // 60:02d}:{gap_minutes % 60:02d}:00+00:00"
            target = make_media(f"target_{gap_minutes}", taken_utc=target_time)
            backfill_gps([before, target], _config())
            return target.gps_confidence

        close = gap_confidence(5)
        far = gap_confidence(60)

        assert close is not None and far is not None
        assert close > far


class TestIdempotentSecondRun:
    def test_running_backfill_twice_does_not_change_already_interpolated_items(
        self, make_media
    ) -> None:
        before = make_media(
            "before",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        target = make_media("target", taken_utc="2026-07-18T10:30:00+00:00")
        after = make_media(
            "after",
            taken_utc="2026-07-18T11:00:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.EXIF,
        )
        media_list = [before, target, after]

        backfill_gps(media_list, _config())
        first_lat, first_lon, first_confidence = target.lat, target.lon, target.gps_confidence

        second_run_filled = backfill_gps(media_list, _config())

        assert second_run_filled == []
        assert target.lat == first_lat
        assert target.lon == first_lon
        assert target.gps_confidence == first_confidence
        assert target.gps_source == GpsSource.INTERPOLATED


class TestDoesNotChainOffPreviouslyInterpolatedPoints:
    def test_interpolated_items_are_not_used_as_anchors_for_others(self, make_media) -> None:
        exif_anchor = make_media(
            "exif_anchor",
            taken_utc="2026-07-18T10:00:00+00:00",
            lat=SALZBURG[0],
            lon=SALZBURG[1],
            gps_source=GpsSource.EXIF,
        )
        already_interpolated = make_media(
            "already_interpolated",
            taken_utc="2026-07-18T10:05:00+00:00",
            lat=VIENNA[0],
            lon=VIENNA[1],
            gps_source=GpsSource.INTERPOLATED,
        )
        far_target = make_media("far_target", taken_utc="2026-07-18T13:00:00+00:00")

        filled = backfill_gps(
            [exif_anchor, already_interpolated, far_target], _config(window_minutes=120.0)
        )

        # far_target is 180 minutes from the EXIF anchor and would be within range of the
        # already-interpolated point if that were (wrongly) treated as an anchor.
        assert filled == []
        assert far_target.gps_source == GpsSource.NONE
