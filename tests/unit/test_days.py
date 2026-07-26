"""Unit tests for day-assignment logic: no DB, no filesystem, no network.

`assign_days` and `find_suspicious_gaps` are pure functions over in-memory `Media` lists, so
they're exercised directly here rather than through `DaysStage`.
"""

from __future__ import annotations

from story_book.pipeline.days import assign_days, find_suspicious_gaps


class TestLateNightStaysWithTheEveningItBegan:
    """The acceptance criterion: a late-night sequence stays with the evening it began, under
    the default `day_start_hour = 4`."""

    def test_2330_and_next_day_0130_land_on_the_same_day(self, make_media) -> None:
        evening = make_media("evening", taken_local="2026-07-19T23:30:00")
        after_midnight = make_media("after_midnight", taken_local="2026-07-20T01:30:00")

        assignments = assign_days([evening, after_midnight], day_start_hour=4)

        assert assignments["evening"] == assignments["after_midnight"] == "2026-07-19"

    def test_item_exactly_at_day_start_hour_starts_a_new_day(self, make_media) -> None:
        media = make_media(taken_local="2026-07-20T04:00:00")

        assignments = assign_days([media], day_start_hour=4)

        assert assignments[media.hash] == "2026-07-20"

    def test_item_one_minute_before_day_start_hour_belongs_to_previous_day(
        self, make_media
    ) -> None:
        media = make_media(taken_local="2026-07-20T03:59:00")

        assignments = assign_days([media], day_start_hour=4)

        assert assignments[media.hash] == "2026-07-19"

    def test_item_well_before_midnight_is_unaffected(self, make_media) -> None:
        media = make_media(taken_local="2026-07-19T14:00:00")

        assignments = assign_days([media], day_start_hour=4)

        assert assignments[media.hash] == "2026-07-19"


class TestDayStartHourZeroBehavesAsPlainCalendarDays:
    def test_items_bucket_by_naive_calendar_date(self, make_media) -> None:
        late = make_media("late", taken_local="2026-07-19T23:59:00")
        early = make_media("early", taken_local="2026-07-20T00:01:00")

        assignments = assign_days([late, early], day_start_hour=0)

        assert assignments["late"] == "2026-07-19"
        assert assignments["early"] == "2026-07-20"

    def test_midnight_exactly_starts_a_new_day(self, make_media) -> None:
        media = make_media(taken_local="2026-07-20T00:00:00")

        assignments = assign_days([media], day_start_hour=0)

        assert assignments[media.hash] == "2026-07-20"


class TestUndatedItems:
    def test_item_with_no_taken_local_is_excluded_from_assignments(self, make_media) -> None:
        undated = make_media(taken_local=None)
        dated = make_media("dated", taken_local="2026-07-19T10:00:00")

        assignments = assign_days([undated, dated], day_start_hour=4)

        assert undated.hash not in assignments
        assert dated.hash in assignments

    def test_all_undated_yields_no_assignments(self, make_media) -> None:
        assignments = assign_days([make_media(taken_local=None)], day_start_hour=4)

        assert assignments == {}


class TestSingleItem:
    def test_a_single_dated_item_gets_its_own_day(self, make_media) -> None:
        media = make_media(taken_local="2026-07-19T10:00:00")

        assignments = assign_days([media], day_start_hour=4)

        assert assignments == {media.hash: "2026-07-19"}


class TestSuspiciousGapDetection:
    def test_a_gap_larger_than_the_threshold_is_reported(self, make_media) -> None:
        before = make_media(
            "before", taken_local="2026-07-19T10:00:00", taken_utc="2026-07-19T10:00:00+00:00"
        )
        after = make_media(
            "after", taken_local="2026-07-25T10:00:00", taken_utc="2026-07-25T10:00:00+00:00"
        )

        gaps = find_suspicious_gaps([before, after], suspicious_gap_days=2.0)

        assert len(gaps) == 1
        assert gaps[0].before.hash == "before"
        assert gaps[0].after.hash == "after"
        assert gaps[0].gap_days == 6.0

    def test_a_gap_at_or_below_the_threshold_is_not_reported(self, make_media) -> None:
        before = make_media(
            "before", taken_local="2026-07-19T10:00:00", taken_utc="2026-07-19T10:00:00+00:00"
        )
        after = make_media(
            "after", taken_local="2026-07-21T10:00:00", taken_utc="2026-07-21T10:00:00+00:00"
        )

        gaps = find_suspicious_gaps([before, after], suspicious_gap_days=2.0)

        assert gaps == []

    def test_gap_detection_orders_by_utc_not_local(self, make_media) -> None:
        """A device reading a far-off local clock must not be mistaken for a gap when its UTC
        instant is actually adjacent to the other item's."""
        first = make_media(
            "first", taken_local="2026-07-19T23:00:00", taken_utc="2026-07-19T21:00:00+00:00"
        )
        second = make_media(
            "second", taken_local="2026-07-20T02:00:00", taken_utc="2026-07-19T23:00:00+00:00"
        )

        gaps = find_suspicious_gaps([first, second], suspicious_gap_days=2.0)

        assert gaps == []

    def test_items_without_taken_utc_are_ignored(self, make_media) -> None:
        no_utc = make_media("no_utc", taken_local="2026-07-19T10:00:00", taken_utc=None)

        gaps = find_suspicious_gaps([no_utc], suspicious_gap_days=2.0)

        assert gaps == []
