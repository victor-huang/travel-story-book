"""Fixture integrity. If these fail, every downstream stage test is untrustworthy."""

from __future__ import annotations

import subprocess
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
    "tz_before_1.jpg",
    "tz_before_2.jpg",
    "tz_before_3.jpg",
    "tz_after_1.jpg",
    "tz_after_2.jpg",
    "tz_after_3.jpg",
    "offset_gps_conflict.jpg",
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


VIDEO_FIXTURES = ["clip_speech.mov", "clip_silent.mp4"]


class TestVideoFixtures:
    """Videos are committed artifacts like the images, so their presence is asserted, not
    skipped. Gating these on ffmpeg was wrong: a clone has the fixtures whether or not it has
    the binary, and the binary-based skip turned a missing fixture into a hard CI failure
    instead of the intended skip. Only the ffprobe-based checks need the binary.
    """

    @pytest.mark.parametrize("name", VIDEO_FIXTURES)
    def test_fixture_exists(self, media_dir: Path, name: str) -> None:
        assert (media_dir / name).exists()

    @pytest.mark.parametrize("name", VIDEO_FIXTURES)
    def test_fixture_stays_small(self, media_dir: Path, name: str) -> None:
        assert (media_dir / name).stat().st_size < 50 * 1024

    @pytest.mark.needs_ffmpeg
    @pytest.mark.parametrize("name", VIDEO_FIXTURES)
    def test_fixture_is_decodable(self, media_dir: Path, name: str, has_ffmpeg: bool) -> None:
        if not has_ffmpeg:
            pytest.skip("ffprobe unavailable")
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_dir / name),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert float(probe.stdout.strip()) == pytest.approx(3.0, abs=0.5)

    @pytest.mark.needs_ffmpeg
    @pytest.mark.parametrize("name", VIDEO_FIXTURES)
    def test_fixture_has_an_audio_track(self, media_dir: Path, name: str, has_ffmpeg: bool) -> None:
        """Both clips carry audio; only one carries speech. T15's auto mode must tell them apart."""
        if not has_ffmpeg:
            pytest.skip("ffprobe unavailable")
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_dir / name),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == "audio"
