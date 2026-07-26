"""Profiler against the real fixture media -- the cases mocked unit tests cannot vouch for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_book.media_types import VIDEO_EXTENSIONS, classify
from story_book.profile import read_metadata, run, scan
from story_book.profile_json import profile_to_dict


def _expected_counts(media_dir: Path) -> tuple[int, int, int]:
    """(media, images, videos) derived from the directory rather than hard-coded.

    Hard-coded counts made every one of these tests fail the moment a fixture was added, which
    is noise: the behaviour under test is "the profiler agrees with what is on disk".
    """
    files = [p for p in media_dir.iterdir() if p.is_file() and classify(p) is not None]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXTENSIONS]
    return len(files), len(files) - len(videos), len(videos)


@pytest.fixture
def profile(media_dir: Path, has_exiftool: bool):
    if not has_exiftool:
        pytest.skip("exiftool not installed")
    return run(media_dir)


class TestScan:
    def test_finds_every_media_file(self, media_dir: Path) -> None:
        paths, _ = scan(media_dir)
        assert len(paths) == _expected_counts(media_dir)[0]

    def test_skips_the_non_media_file(self, media_dir: Path) -> None:
        paths, _ = scan(media_dir)
        assert not any(p.name == "notes.txt" for p in paths)

    def test_counts_what_it_skipped(self, media_dir: Path) -> None:
        _, ignored = scan(media_dir)
        assert ignored == 1

    def test_recurses_into_subdirectories(self, source_dir: Path) -> None:
        nested = source_dir / "Day02"
        nested.mkdir()
        (source_dir / "sharp.jpg").rename(nested / "sharp.jpg")
        paths, _ = scan(source_dir)
        assert any(p.parent.name == "Day02" for p in paths)

    def test_ignores_hidden_directories(self, source_dir: Path) -> None:
        hidden = source_dir / ".Trash"
        hidden.mkdir()
        (hidden / "deleted.jpg").write_bytes(b"x")
        paths, _ = scan(source_dir)
        assert not any(".Trash" in p.parts for p in paths)

    def test_is_deterministic(self, media_dir: Path) -> None:
        assert scan(media_dir)[0] == scan(media_dir)[0]


class TestReadMetadata:
    def test_reads_every_path(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        paths, _ = scan(media_dir)
        assert len(read_metadata(paths)) == len(paths)

    def test_reads_the_heic_offset_tag(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "heic_gps_offset.heic"
        assert read_metadata([target])[str(target)]["OffsetTimeOriginal"] == "+02:00"

    def test_reads_video_duration(self, media_dir: Path, has_exiftool: bool) -> None:
        """Regression: -fast2 silently zeroed this by skipping the moov atom."""
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "clip_speech.mov"
        assert read_metadata([target])[str(target)]["Duration"] == pytest.approx(3.0, abs=0.5)

    def test_no_exif_fixture_yields_no_timestamp(self, media_dir: Path, has_exiftool: bool) -> None:
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        target = media_dir / "jpeg_no_exif.jpg"
        assert "DateTimeOriginal" not in read_metadata([target])[str(target)]

    def test_empty_input_returns_empty(self) -> None:
        assert read_metadata([]) == {}


class TestRunOnFixtures:
    def test_counts_images(self, profile, media_dir: Path) -> None:
        assert profile.images == _expected_counts(media_dir)[1]

    def test_counts_videos(self, profile, media_dir: Path) -> None:
        assert profile.videos == _expected_counts(media_dir)[2]

    def test_sums_video_duration(self, profile, media_dir: Path) -> None:
        """Every video fixture is a few seconds, so the total scales with how many there are."""
        assert profile.video_seconds >= 2.0 * _expected_counts(media_dir)[2] - 1.0

    def test_identifies_the_iphone(self, profile) -> None:
        assert profile.devices["Apple iPhone 16 Pro"] > 0

    def test_identifies_the_sony_without_gps(self, profile) -> None:
        assert profile.device_gps.get("Sony ILCE-7M4", 0) == 0

    def test_detects_the_timezone_crossing(self, profile) -> None:
        assert profile.timezone_crossings >= 1

    def test_reports_both_utc_offsets(self, profile) -> None:
        assert "+02:00" in profile.offsets and "+03:00" in profile.offsets

    def test_counts_items_lacking_an_offset_tag(self, profile) -> None:
        assert profile.offsets["(none)"] > 0

    def test_finds_the_no_exif_fixture_as_undated(self, profile) -> None:
        assert profile.without_timestamp > 0

    def test_reports_missing_gps(self, profile) -> None:
        assert profile.without_gps > 0

    def test_date_range_matches_the_fixtures(self, profile) -> None:
        assert profile.first.date().isoformat() == "2026-07-18"

    def test_detects_heic(self, profile) -> None:
        assert profile.extensions[".heic"] == 1

    def test_warns_about_the_timezone_crossing(self, profile) -> None:
        from story_book.profile import warnings

        assert any("offset change" in w for w in warnings(profile))

    def test_suggests_config_values(self, profile) -> None:
        from story_book.profile import suggestions

        assert len(suggestions(profile)) >= 3

    def test_writes_nothing_to_the_source(self, source_dir: Path, has_exiftool: bool) -> None:
        """The non-destructive guarantee, at the smallest scale it can be checked."""
        if not has_exiftool:
            pytest.skip("exiftool not installed")
        before = {p: p.read_bytes() for p in sorted(source_dir.rglob("*")) if p.is_file()}
        run(source_dir)
        after = {p: p.read_bytes() for p in sorted(source_dir.rglob("*")) if p.is_file()}
        assert before == after


class TestProfileJson:
    def test_is_serializable(self, profile) -> None:
        assert json.loads(json.dumps(profile_to_dict(profile)))

    def test_includes_suggested_config(self, profile) -> None:
        assert profile_to_dict(profile)["suggested_config"]

    def test_includes_gap_percentiles(self, profile) -> None:
        assert "p90" in profile_to_dict(profile)["time"]["gaps_minutes"]

    def test_records_the_source_path(self, profile, media_dir: Path) -> None:
        assert profile_to_dict(profile)["source"] == str(media_dir)


class TestDegradesWithoutExiftool:
    """Availability is now decided by `story_book.exif.exiftool_available`, which the profiler
    imports -- so that is what these patch."""

    def test_still_counts_files(self, media_dir: Path, mocker) -> None:
        mocker.patch("story_book.profile.exiftool_available", return_value=False)
        assert run(media_dir).total == _expected_counts(media_dir)[0]

    def test_reports_exiftool_as_unavailable(self, media_dir: Path, mocker) -> None:
        mocker.patch("story_book.profile.exiftool_available", return_value=False)
        assert run(media_dir).exiftool_available is False

    def test_warns_that_metadata_is_missing(self, media_dir: Path, mocker) -> None:
        from story_book.profile import warnings

        mocker.patch("story_book.profile.exiftool_available", return_value=False)
        assert any("exiftool not found" in w for w in warnings(run(media_dir)))


class TestEmptyFolder:
    def test_produces_a_zero_profile(self, tmp_path: Path) -> None:
        assert run(tmp_path).total == 0

    def test_does_not_crash_on_suggestions(self, tmp_path: Path) -> None:
        from story_book.profile import suggestions

        assert suggestions(run(tmp_path)) == []


class TestOffsetGpsConflict:
    """The offset_gps_conflict fixture is Vienna coordinates tagged -07:00 -- nine hours wrong,
    which would place it on the wrong day. Real exports contain these.
    """

    def test_the_conflict_is_detected(self, profile) -> None:
        assert profile.offset_conflicts >= 1

    def test_the_offending_file_is_named(self, profile) -> None:
        assert "offset_gps_conflict.jpg" in profile.conflict_examples

    def test_it_is_warned_about(self, profile) -> None:
        from story_book.profile import warnings

        assert any("disagrees" in w for w in warnings(profile))

    def test_the_warning_names_t12(self, profile) -> None:
        from story_book.profile import warnings

        assert any("T12" in w for w in warnings(profile))

    def test_the_json_reports_it(self, profile) -> None:
        assert profile_to_dict(profile)["time"]["offset_gps_conflicts"] >= 1
