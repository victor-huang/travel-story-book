from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from story_book.db.models import MediaKind
from story_book.media_types import classify, is_hidden
from story_book.profile import (
    GapStats,
    Item,
    Profile,
    _count_crossings,
    _format_offset,
    _gap_stats,
    _largest_gap_days,
    _parse_exif_datetime,
    _parse_offset,
    analyze,
    build_item,
    percentile,
    suggestions,
    warnings,
)
from story_book.profile_render import human_bytes, human_duration


def item(
    name: str = "a.jpg",
    *,
    kind: MediaKind = MediaKind.IMAGE,
    taken: datetime | None = None,
    offset: int | None = None,
    gps: bool = False,
    device: str | None = "Apple iPhone 16 Pro",
    size: int = 1000,
    duration: float | None = None,
) -> Item:
    return Item(
        path=Path(f"/src/{name}"),
        kind=kind,
        bytes=size,
        device=device,
        taken=taken,
        utc_offset_minutes=offset,
        has_gps=gps,
        duration=duration,
    )


class TestClassify:
    @pytest.mark.parametrize("name", ["a.jpg", "a.JPEG", "a.heic", "a.dng", "a.png"])
    def test_image_extensions_are_images(self, name: str) -> None:
        assert classify(Path(name)) is MediaKind.IMAGE

    @pytest.mark.parametrize("name", ["a.mov", "a.MP4", "a.m4v", "a.avi"])
    def test_video_extensions_are_videos(self, name: str) -> None:
        assert classify(Path(name)) is MediaKind.VIDEO

    @pytest.mark.parametrize("name", ["a.txt", "a.xmp", "a", "a.jpg.bak"])
    def test_other_extensions_are_not_media(self, name: str) -> None:
        assert classify(Path(name)) is None

    def test_dotfile_is_hidden(self) -> None:
        assert is_hidden(Path(".DS_Store"))

    def test_file_in_a_dot_directory_is_hidden(self) -> None:
        assert is_hidden(Path(".Trash/photo.jpg"))

    def test_ordinary_path_is_not_hidden(self) -> None:
        assert not is_hidden(Path("Day01/photo.jpg"))


class TestParseExifDatetime:
    def test_standard_exif_format(self) -> None:
        assert _parse_exif_datetime("2026:07:18 09:20:00") == datetime(2026, 7, 18, 9, 20)

    def test_trailing_offset_is_ignored(self) -> None:
        assert _parse_exif_datetime("2026:07:18 09:20:00+02:00") == datetime(2026, 7, 18, 9, 20)

    def test_zero_date_is_rejected(self) -> None:
        assert _parse_exif_datetime("0000:00:00 00:00:00") is None

    def test_blank_is_rejected(self) -> None:
        assert _parse_exif_datetime("") is None

    def test_non_string_is_rejected(self) -> None:
        assert _parse_exif_datetime(12345) is None

    def test_garbage_is_rejected(self) -> None:
        assert _parse_exif_datetime("not a date") is None


class TestParseOffset:
    def test_positive_offset(self) -> None:
        assert _parse_offset("+02:00") == 120

    def test_negative_offset(self) -> None:
        assert _parse_offset("-08:00") == -480

    def test_half_hour_offset(self) -> None:
        assert _parse_offset("+05:30") == 330

    def test_missing_sign_is_rejected(self) -> None:
        assert _parse_offset("02:00") is None

    def test_none_is_rejected(self) -> None:
        assert _parse_offset(None) is None

    def test_round_trips_through_format(self) -> None:
        assert _format_offset(_parse_offset("+05:30")) == "+05:30"

    def test_negative_round_trips(self) -> None:
        assert _format_offset(-480) == "-08:00"


class TestBuildItem:
    def test_device_joins_make_and_model(self) -> None:
        built = build_item(Path("a.jpg"), {"Make": "Apple", "Model": "iPhone 16 Pro"}, 10)
        assert built.device == "Apple iPhone 16 Pro"

    def test_missing_device_is_none(self) -> None:
        assert build_item(Path("a.jpg"), {}, 10).device is None

    def test_falls_back_to_create_date(self) -> None:
        built = build_item(Path("a.jpg"), {"CreateDate": "2026:07:18 09:20:00"}, 10)
        assert built.taken == datetime(2026, 7, 18, 9, 20)

    def test_prefers_date_time_original_over_create_date(self) -> None:
        built = build_item(
            Path("a.jpg"),
            {"DateTimeOriginal": "2026:07:18 09:20:00", "CreateDate": "2020:01:01 00:00:00"},
            10,
        )
        assert built.taken == datetime(2026, 7, 18, 9, 20)

    def test_gps_requires_both_coordinates(self) -> None:
        assert build_item(Path("a.jpg"), {"GPSLatitude": 47.8}, 10).has_gps is False

    def test_gps_is_detected_when_both_present(self) -> None:
        built = build_item(Path("a.jpg"), {"GPSLatitude": 47.8, "GPSLongitude": 13.0}, 10)
        assert built.has_gps is True

    def test_video_duration_is_read(self) -> None:
        assert build_item(Path("a.mov"), {"Duration": 3.0}, 10).duration == 3.0

    def test_non_numeric_duration_is_ignored(self) -> None:
        assert build_item(Path("a.mov"), {"Duration": "0:00:03"}, 10).duration is None


class TestPercentile:
    def test_median_of_odd_length(self) -> None:
        assert percentile([1, 2, 3], 0.5) == 2

    def test_max_at_full_fraction(self) -> None:
        assert percentile([1, 2, 3, 100], 1.0) == 100

    def test_min_at_zero_fraction(self) -> None:
        assert percentile([5, 1, 3], 0.0) == 1

    def test_empty_returns_zero(self) -> None:
        assert percentile([], 0.9) == 0.0


class TestGapStats:
    def test_counts_gaps_between_consecutive_items(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, hour)) for hour in (9, 10, 11)]
        assert _gap_stats(items).count == 2

    def test_gap_is_measured_in_minutes(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, 9)), item(taken=datetime(2026, 7, 18, 10, 30))]
        assert _gap_stats(items).largest == 90.0

    def test_single_item_has_no_gaps(self) -> None:
        assert _gap_stats([item(taken=datetime(2026, 7, 18, 9))]) == GapStats()

    def test_largest_gap_in_days(self) -> None:
        items = [item(taken=datetime(2026, 7, 18)), item(taken=datetime(2026, 7, 20, 12))]
        assert _largest_gap_days(items) == pytest.approx(2.5)


class TestTimezoneCrossings:
    def test_a_single_offset_is_no_crossing(self) -> None:
        items = [item(offset=120), item(offset=120)]
        assert _count_crossings(items) == 0

    def test_a_change_counts_as_one_crossing(self) -> None:
        items = [item(offset=120), item(offset=180)]
        assert _count_crossings(items) == 1

    def test_items_without_an_offset_are_ignored(self) -> None:
        items = [item(offset=120), item(offset=None), item(offset=120)]
        assert _count_crossings(items) == 0

    def test_two_changes_count_separately(self) -> None:
        items = [item(offset=120), item(offset=180), item(offset=120)]
        assert _count_crossings(items) == 2


class TestAnalyze:
    def test_counts_images_and_videos_separately(self) -> None:
        result = analyze(Path("/src"), [item(), item("v.mov", kind=MediaKind.VIDEO)], 0, True)
        assert (result.images, result.videos) == (1, 1)

    def test_sums_bytes(self) -> None:
        result = analyze(Path("/src"), [item(size=100), item(size=250)], 0, True)
        assert result.total_bytes == 350

    def test_sums_video_duration(self) -> None:
        items = [item("v.mov", kind=MediaKind.VIDEO, duration=3.5)]
        assert analyze(Path("/src"), items, 0, True).video_seconds == 3.5

    def test_tracks_gps_per_device(self) -> None:
        items = [
            item(gps=True, device="A"),
            item(gps=False, device="A"),
            item(gps=True, device="B"),
        ]
        result = analyze(Path("/src"), items, 0, True)
        assert result.device_gps == {"A": 1, "B": 1}

    def test_counts_items_without_gps(self) -> None:
        result = analyze(Path("/src"), [item(gps=True), item(gps=False)], 0, True)
        assert result.without_gps == 1

    def test_gps_coverage_is_a_fraction(self) -> None:
        result = analyze(Path("/src"), [item(gps=True), item(gps=False)], 0, True)
        assert result.gps_coverage == 0.5

    def test_unknown_device_is_labelled(self) -> None:
        result = analyze(Path("/src"), [item(device=None)], 0, True)
        assert result.devices["(unknown device)"] == 1

    def test_counts_items_without_a_timestamp(self) -> None:
        result = analyze(Path("/src"), [item(taken=None)], 0, True)
        assert result.without_timestamp == 1

    def test_span_days_is_inclusive(self) -> None:
        items = [item(taken=datetime(2026, 7, 18)), item(taken=datetime(2026, 7, 20))]
        assert analyze(Path("/src"), items, 0, True).span_days == 3

    def test_dates_with_media_are_deduplicated(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, 9)), item(taken=datetime(2026, 7, 18, 20))]
        assert len(analyze(Path("/src"), items, 0, True).local_dates) == 1

    def test_late_night_items_are_counted(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, 1)), item(taken=datetime(2026, 7, 18, 13))]
        assert analyze(Path("/src"), items, 0, True).late_night_items == 1

    def test_first_and_last_ignore_input_order(self) -> None:
        items = [item(taken=datetime(2026, 7, 20)), item(taken=datetime(2026, 7, 18))]
        assert analyze(Path("/src"), items, 0, True).first == datetime(2026, 7, 18)

    def test_heic_share_is_relative_to_images(self) -> None:
        items = [item("a.heic"), item("b.jpg")]
        assert analyze(Path("/src"), items, 0, True).heic_share == 0.5

    def test_empty_input_produces_an_empty_profile(self) -> None:
        result = analyze(Path("/src"), [], 3, True)
        assert result.total == 0 and result.ignored_files == 3


class TestWarnings:
    def test_missing_exiftool_is_warned(self) -> None:
        result = analyze(Path("/src"), [item()], 0, False)
        assert any("exiftool not found" in w for w in warnings(result))

    def test_empty_folder_is_warned(self) -> None:
        assert any("no importable media" in w for w in warnings(analyze(Path("/s"), [], 0, True)))

    def test_timezone_crossing_is_warned(self) -> None:
        items = [
            item(taken=datetime(2026, 7, 18, 9), offset=120),
            item(taken=datetime(2026, 7, 18, 10), offset=180),
        ]
        result = analyze(Path("/src"), items, 0, True)
        assert any("offset change" in w for w in warnings(result))

    def test_a_device_with_no_gps_at_all_is_warned(self) -> None:
        items = [item(device="Sony ILCE-7M4", gps=False) for _ in range(10)]
        result = analyze(Path("/src"), items, 0, True)
        assert any("no GPS at all" in w for w in warnings(result))

    def test_a_small_device_sample_is_not_warned(self) -> None:
        items = [item(device="Sony ILCE-7M4", gps=False) for _ in range(3)]
        result = analyze(Path("/src"), items, 0, True)
        assert not any("no GPS at all" in w for w in warnings(result))

    def test_a_multi_day_gap_suggests_two_trips(self) -> None:
        items = [item(taken=datetime(2026, 7, 18)), item(taken=datetime(2026, 7, 25))]
        result = analyze(Path("/src"), items, 0, True)
        assert any("more than one trip" in w for w in warnings(result))

    def test_missing_timestamps_are_warned(self) -> None:
        result = analyze(Path("/src"), [item(taken=None)], 0, True)
        assert any("no usable timestamp" in w for w in warnings(result))


class TestSuggestions:
    def test_gap_minutes_is_suggested_from_observed_gaps(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, hour)) for hour in range(9, 18)]
        keys = [key for key, _, _ in suggestions(analyze(Path("/s"), items, 0, True))]
        assert "events.gap_minutes" in keys

    def test_gap_suggestion_is_clamped_to_a_sane_floor(self) -> None:
        items = [item(taken=datetime(2026, 7, 18, 9, minute)) for minute in range(0, 30)]
        found = dict(
            (key, value) for key, value, _ in suggestions(analyze(Path("/s"), items, 0, True))
        )
        assert float(found["events.gap_minutes"]) >= 30

    def test_gap_suggestion_is_clamped_to_a_sane_ceiling(self) -> None:
        items = [item(taken=datetime(2026, 7, 18 + day)) for day in range(0, 5)]
        found = dict(
            (key, value) for key, value, _ in suggestions(analyze(Path("/s"), items, 0, True))
        )
        assert float(found["events.gap_minutes"]) <= 240

    def test_suspicious_gap_is_suggested_above_the_observed_gap(self) -> None:
        items = [item(taken=datetime(2026, 7, 18)), item(taken=datetime(2026, 7, 20))]
        found = dict(
            (key, value) for key, value, _ in suggestions(analyze(Path("/s"), items, 0, True))
        )
        assert float(found["time.suspicious_gap_days"]) > 2.0

    def test_transcribe_is_suggested_when_videos_exist(self) -> None:
        items = [item("v.mov", kind=MediaKind.VIDEO, duration=30)]
        keys = [key for key, _, _ in suggestions(analyze(Path("/s"), items, 0, True))]
        assert "video.transcribe" in keys

    def test_no_video_suggestion_without_videos(self) -> None:
        keys = [key for key, _, _ in suggestions(analyze(Path("/s"), [item()], 0, True))]
        assert "video.transcribe" not in keys

    def test_every_suggestion_names_a_real_config_key(self) -> None:
        """A suggestion pointing at a key that does not exist is worse than no suggestion."""
        from story_book.config import Config

        items = [
            item(taken=datetime(2026, 7, 18, 1), gps=False),
            item("v.mov", kind=MediaKind.VIDEO, taken=datetime(2026, 7, 20, 9), duration=30),
        ]
        config = Config()
        for key, _, _ in suggestions(analyze(Path("/s"), items, 0, True)):
            section, field_name = key.split(".")
            assert hasattr(getattr(config, section), field_name), key

    def test_an_empty_profile_suggests_nothing(self) -> None:
        assert suggestions(Profile(source=Path("/s"))) == []


class TestRenderHelpers:
    def test_bytes_in_kilobytes(self) -> None:
        assert human_bytes(2048) == "2.0 KB"

    def test_bytes_in_gigabytes(self) -> None:
        assert human_bytes(3 * 1024**3) == "3.0 GB"

    def test_small_byte_counts_have_no_decimal(self) -> None:
        assert human_bytes(512) == "512 B"

    def test_duration_under_a_minute(self) -> None:
        assert human_duration(42) == "42s"

    def test_duration_in_minutes(self) -> None:
        assert human_duration(150) == "2m 30s"

    def test_duration_in_hours(self) -> None:
        assert human_duration(7800) == "2h 10m"
