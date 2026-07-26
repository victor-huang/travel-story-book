"""Canonical EXIF parsing and field priority.

These tests moved here from `test_profile.py` when the profiler was migrated onto this module.
The rules they cover are binding amendments to Module 2 that came out of real-data profiling, so
they belong next to the single implementation rather than beside one of its two callers.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from story_book.db.models import MediaKind
from story_book.exif import (
    IMAGE_FIELD_PRIORITY,
    VIDEO_FIELD_PRIORITY,
    embedded_offset,
    extract_timestamp,
    field_priority,
    parse_exif_datetime,
    parse_offset,
)


class TestParseExifDatetime:
    def test_standard_exif_format(self) -> None:
        assert parse_exif_datetime("2026:07:18 09:20:00") == datetime(2026, 7, 18, 9, 20)

    def test_trailing_offset_is_ignored(self) -> None:
        assert parse_exif_datetime("2026:07:18 09:20:00+02:00") == datetime(2026, 7, 18, 9, 20)

    def test_zero_date_is_rejected(self) -> None:
        """ffmpeg writes this placeholder; treating it as a date would fabricate a timestamp."""
        assert parse_exif_datetime("0000:00:00 00:00:00") is None

    def test_blank_is_rejected(self) -> None:
        assert parse_exif_datetime("") is None

    def test_non_string_is_rejected(self) -> None:
        assert parse_exif_datetime(12345) is None

    def test_garbage_is_rejected(self) -> None:
        assert parse_exif_datetime("not a date") is None


class TestParseOffset:
    def test_positive_offset(self) -> None:
        assert parse_offset("+02:00") == 120

    def test_negative_offset(self) -> None:
        assert parse_offset("-08:00") == -480

    def test_half_hour_offset(self) -> None:
        assert parse_offset("+05:30") == 330

    def test_missing_sign_is_rejected(self) -> None:
        assert parse_offset("02:00") is None

    def test_none_is_rejected(self) -> None:
        assert parse_offset(None) is None


class TestEmbeddedOffset:
    def test_offset_is_read_from_the_tail_of_a_timestamp(self) -> None:
        assert embedded_offset("2026:07:18 11:37:58+02:00") == 120

    def test_a_bare_timestamp_has_no_embedded_offset(self) -> None:
        assert embedded_offset("2026:07:18 11:37:58") is None


class TestFieldPriority:
    def test_video_prefers_quicktime_creation_date(self) -> None:
        """The P01 finding: CreateDate on a Photos-exported .mov is the *export* time."""
        assert field_priority(MediaKind.VIDEO)[0] == "CreationDate"

    def test_image_prefers_date_time_original(self) -> None:
        assert field_priority(MediaKind.IMAGE)[0] == "DateTimeOriginal"

    def test_the_two_orders_differ(self) -> None:
        assert VIDEO_FIELD_PRIORITY != IMAGE_FIELD_PRIORITY

    def test_both_orders_cover_the_same_fields(self) -> None:
        assert set(VIDEO_FIELD_PRIORITY) == set(IMAGE_FIELD_PRIORITY)


class TestExtractTimestamp:
    def test_video_creation_date_beats_export_create_date(self) -> None:
        meta = {"CreationDate": "2026:07:18 11:37:58+02:00", "CreateDate": "2026:07:26 18:43:20"}
        assert extract_timestamp(meta, MediaKind.VIDEO).dt == datetime(2026, 7, 18, 11, 37, 58)

    def test_the_source_field_is_recorded(self) -> None:
        meta = {"CreationDate": "2026:07:18 11:37:58+02:00", "CreateDate": "2026:07:26 18:43:20"}
        assert extract_timestamp(meta, MediaKind.VIDEO).field == "CreationDate"

    def test_offset_comes_from_the_embedded_timestamp_when_no_tag(self) -> None:
        meta = {"CreationDate": "2026:07:18 11:37:58+02:00"}
        assert extract_timestamp(meta, MediaKind.VIDEO).offset_minutes == 120

    def test_explicit_offset_tag_wins_over_embedded(self) -> None:
        meta = {
            "DateTimeOriginal": "2026:07:18 09:20:00",
            "OffsetTimeOriginal": "+03:00",
            "CreationDate": "2026:07:18 11:37:58+02:00",
        }
        assert extract_timestamp(meta, MediaKind.IMAGE).offset_minutes == 180

    def test_a_video_falling_back_to_create_date_is_flagged(self) -> None:
        """So an export artifact can be warned about instead of silently trusted."""
        meta = {"CreateDate": "2026:07:26 18:43:20"}
        assert extract_timestamp(meta, MediaKind.VIDEO).is_export_artifact is True

    def test_a_video_using_creation_date_is_not_flagged(self) -> None:
        meta = {"CreationDate": "2026:07:18 11:37:58+02:00"}
        assert extract_timestamp(meta, MediaKind.VIDEO).is_export_artifact is False

    def test_no_usable_field_yields_no_datetime(self) -> None:
        assert extract_timestamp({}, MediaKind.IMAGE).dt is None

    @pytest.mark.parametrize("kind", [MediaKind.IMAGE, MediaKind.VIDEO])
    def test_placeholder_dates_are_not_accepted(self, kind: MediaKind) -> None:
        meta = {"CreateDate": "0000:00:00 00:00:00", "DateTimeOriginal": "0000:00:00 00:00:00"}
        assert extract_timestamp(meta, kind).dt is None
