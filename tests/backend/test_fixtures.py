"""Fixture integrity. If these fail, every downstream stage test is untrustworthy."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

IMAGE_FIXTURES = [
    "heic_gps_offset.heic",
    "jpeg_gps_no_offset.jpg",
    "jpeg_no_gps.jpg",
    "jpeg_no_exif.jpg",
    "burst_a.jpg",
    "burst_b.jpg",
    "exact_a.jpg",
    "exact_b.jpg",
    "distinct_a.jpg",
    "distinct_b.jpg",
    "sharp.jpg",
    "blurred.jpg",
    "screenshot.jpg",
    "receipt.jpg",
    "overexposed.jpg",
    "underexposed.jpg",
    "tz_before.jpg",
    "tz_after.jpg",
]


class TestFixtureMedia:
    @pytest.mark.parametrize("name", IMAGE_FIXTURES)
    def test_fixture_exists(self, media_dir: Path, name: str) -> None:
        assert (media_dir / name).exists()

    @pytest.mark.parametrize("name", IMAGE_FIXTURES)
    def test_fixture_stays_small(self, media_dir: Path, name: str) -> None:
        assert (media_dir / name).stat().st_size < 50 * 1024

    @pytest.mark.parametrize("name", [n for n in IMAGE_FIXTURES if n.endswith(".jpg")])
    def test_jpeg_decodes(self, media_dir: Path, name: str) -> None:
        with Image.open(media_dir / name) as image:
            image.load()
            assert image.size[0] > 0

    def test_heic_decodes(self, media_dir: Path) -> None:
        """The dependency most likely to break on a fresh machine. Required by T05."""
        import pillow_heif

        pillow_heif.register_heif_opener()
        with Image.open(media_dir / "heic_gps_offset.heic") as image:
            image.load()
            assert image.size == (320, 240)

    def test_exact_duplicates_have_identical_bytes(self, media_dir: Path) -> None:
        assert (media_dir / "exact_a.jpg").read_bytes() == (media_dir / "exact_b.jpg").read_bytes()

    def test_distinct_fixtures_have_different_bytes(self, media_dir: Path) -> None:
        assert (media_dir / "distinct_a.jpg").read_bytes() != (
            media_dir / "distinct_b.jpg"
        ).read_bytes()

    def test_non_media_file_is_present_for_scanner_tests(self, media_dir: Path) -> None:
        assert (media_dir / "notes.txt").exists()


class TestVideoFixtures:
    def test_speech_clip_exists(self, media_dir: Path, has_ffmpeg: bool) -> None:
        if not has_ffmpeg:
            pytest.skip("ffmpeg not installed; video fixtures not generated")
        assert (media_dir / "clip_speech.mov").exists()

    def test_silent_clip_exists(self, media_dir: Path, has_ffmpeg: bool) -> None:
        if not has_ffmpeg:
            pytest.skip("ffmpeg not installed; video fixtures not generated")
        assert (media_dir / "clip_silent.mp4").exists()
