"""Unit tests for the metadata stage: no DB, no filesystem, no network.

The real exiftool subprocess and the real sqlite connection are both mocked; behavior against
real fixtures belongs in `tests/backend/test_metadata.py`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from story_book.db.models import GpsSource, Media, MediaKind
from story_book.exif import run_exiftool
from story_book.pipeline.metadata import MetadataStage, _as_int, _as_number, _device_id


class TestDeviceId:
    def test_combines_make_and_model(self) -> None:
        assert _device_id("Apple", "iPhone 16 Pro") == "Apple iPhone 16 Pro"

    def test_missing_model_uses_make_only(self) -> None:
        assert _device_id("Sony", None) == "Sony"

    def test_both_missing_returns_none(self) -> None:
        assert _device_id(None, None) is None

    def test_blank_strings_are_treated_as_missing(self) -> None:
        assert _device_id("   ", "iPhone") == "iPhone"


class TestAsNumber:
    def test_accepts_int(self) -> None:
        assert _as_number(3) == 3.0

    def test_accepts_float(self) -> None:
        assert _as_number(47.8095) == pytest.approx(47.8095)

    def test_rejects_bool(self) -> None:
        # bool is a subclass of int; a stray True/False must not masquerade as 1.0/0.0.
        assert _as_number(True) is None

    def test_rejects_non_numeric_string(self) -> None:
        assert _as_number("garbage") is None

    def test_none_passes_through(self) -> None:
        assert _as_number(None) is None


class TestAsInt:
    def test_truncates_float(self) -> None:
        assert _as_int(1920.0) == 1920

    def test_none_passes_through(self) -> None:
        assert _as_int(None) is None


class TestMetadataStageAvailable:
    def test_unavailable_when_exiftool_missing(self, mocker) -> None:
        mocker.patch("story_book.pipeline.metadata.exiftool_available", return_value=False)

        available, reason = MetadataStage().available(MagicMock())

        assert available is False
        assert "exiftool" in reason

    def test_available_when_exiftool_present(self, mocker) -> None:
        mocker.patch("story_book.pipeline.metadata.exiftool_available", return_value=True)

        available, reason = MetadataStage().available(MagicMock())

        assert available is True
        assert reason == ""


class TestMetadataStageSelect:
    def test_select_returns_every_media_row(self, mocker) -> None:
        rows = [MagicMock(spec=Media), MagicMock(spec=Media)]
        mocker.patch("story_book.pipeline.metadata.db.iter_media", return_value=iter(rows))

        result = MetadataStage().select(MagicMock())

        assert result == rows


def _media(hash_: str = "h1", path: str = "/src/a.jpg", kind: MediaKind = MediaKind.IMAGE) -> Media:
    return Media(hash=hash_, path=path, kind=kind, bytes=100, mtime=0.0)


class TestMetadataStageProcessBatch:
    def test_populates_timestamp_dimensions_device_and_gps(self, mocker) -> None:
        media = _media()
        raw = {
            "/src/a.jpg": {
                "SourceFile": "/src/a.jpg",
                "DateTimeOriginal": "2026:07:18 09:20:00",
                "Make": "Apple",
                "Model": "iPhone 16 Pro",
                "GPSLatitude": 47.8095,
                "GPSLongitude": 13.0550,
                "GPSAltitude": 424,
                "ImageWidth": 320,
                "ImageHeight": 240,
            }
        }
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value=raw)
        ctx = MagicMock(conn=MagicMock())

        results = MetadataStage().process_batch(ctx, [media])

        assert results == {"h1": True}
        assert media.taken_local == "2026-07-18T09:20:00"
        assert media.width == 320
        assert media.height == 240
        assert media.device_id == "Apple iPhone 16 Pro"
        assert media.gps_source == GpsSource.EXIF
        assert media.lat == pytest.approx(47.8095)
        assert media.altitude == pytest.approx(424.0)

    def test_persists_via_upsert_media(self, mocker) -> None:
        media = _media()
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value={})
        upsert = mocker.patch("story_book.pipeline.metadata.db.upsert_media")
        ctx = MagicMock(conn=MagicMock())

        MetadataStage().process_batch(ctx, [media])

        upsert.assert_called_once_with(ctx.conn, media)

    def test_missing_exif_produces_nulls_without_raising(self, mocker) -> None:
        media = _media(hash_="h2", path="/src/none.jpg")
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value={})
        ctx = MagicMock(conn=MagicMock())

        results = MetadataStage().process_batch(ctx, [media])

        assert results == {"h2": True}
        assert media.taken_local is None
        assert media.width is None
        assert media.height is None
        assert media.duration is None
        assert media.device_id is None
        assert media.lat is None
        assert media.lon is None
        assert media.altitude is None
        assert media.gps_source == GpsSource.NONE

    def test_leaves_timezone_fields_untouched(self, mocker) -> None:
        media = _media(hash_="h4")
        media.tz_name = "Europe/Vienna"
        raw = {
            "/src/a.jpg": {"SourceFile": "/src/a.jpg", "DateTimeOriginal": "2026:07:18 09:20:00"}
        }
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value=raw)
        ctx = MagicMock(conn=MagicMock())

        MetadataStage().process_batch(ctx, [media])

        assert media.tz_name == "Europe/Vienna"
        assert media.taken_utc is None

    def test_upserts_device_row_when_make_or_model_present(self, mocker) -> None:
        media = _media()
        raw = {"/src/a.jpg": {"SourceFile": "/src/a.jpg", "Make": "Sony", "Model": "ILCE-7M4"}}
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value=raw)
        conn = MagicMock()
        ctx = MagicMock(conn=conn)

        MetadataStage().process_batch(ctx, [media])

        device_calls = [c for c in conn.execute.call_args_list if "INSERT INTO device" in c.args[0]]
        assert len(device_calls) == 1
        assert device_calls[0].args[1] == ("Sony ILCE-7M4", "Sony", "ILCE-7M4")

    def test_no_device_row_written_without_make_or_model(self, mocker) -> None:
        media = _media()
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value={})
        conn = MagicMock()
        ctx = MagicMock(conn=conn)

        MetadataStage().process_batch(ctx, [media])

        device_calls = [c for c in conn.execute.call_args_list if "INSERT INTO device" in c.args[0]]
        assert device_calls == []

    def test_video_field_priority_prefers_creation_date_over_create_date(self, mocker) -> None:
        media = _media(hash_="h5", path="/src/v.mov", kind=MediaKind.VIDEO)
        raw = {
            "/src/v.mov": {
                "SourceFile": "/src/v.mov",
                "CreationDate": "2026:07:18 09:20:00",
                "CreateDate": "2026:07:22 00:00:00",
                "Duration": 3.0,
            }
        }
        mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value=raw)
        ctx = MagicMock(conn=MagicMock())

        MetadataStage().process_batch(ctx, [media])

        assert media.taken_local == "2026-07-18T09:20:00"
        assert media.duration == 3.0

    def test_image_and_video_batched_in_one_exiftool_call(self, mocker) -> None:
        image = _media(hash_="i1", path="/src/i.jpg", kind=MediaKind.IMAGE)
        video = _media(hash_="v1", path="/src/v.mov", kind=MediaKind.VIDEO)
        run_exiftool = mocker.patch("story_book.pipeline.metadata.run_exiftool", return_value={})
        ctx = MagicMock(conn=MagicMock())

        MetadataStage().process_batch(ctx, [image, video])

        assert run_exiftool.call_count == 1
        called_paths = run_exiftool.call_args.args[0]
        assert {str(p) for p in called_paths} == {"/src/i.jpg", "/src/v.mov"}


class TestRunExiftoolNeverUsesFast2:
    """Regression for the binding P01 finding: -fast2 skips the moov atom and silently zeroes
    video Duration with no error."""

    def test_subprocess_command_excludes_fast2(self, mocker) -> None:
        completed = MagicMock(stdout='[{"SourceFile": "/a.jpg"}]')
        mocker.patch("story_book.exif.exiftool_available", return_value=True)
        run = mocker.patch("story_book.exif.subprocess.run", return_value=completed)

        run_exiftool([Path("/a.jpg")])

        command = run.call_args.args[0]
        assert "-fast2" not in command

    def test_paths_are_fed_over_stdin_not_argv(self, mocker) -> None:
        completed = MagicMock(stdout="[]")
        mocker.patch("story_book.exif.exiftool_available", return_value=True)
        run = mocker.patch("story_book.exif.subprocess.run", return_value=completed)

        run_exiftool([Path("/a.jpg"), Path("/b.jpg")])

        command = run.call_args.args[0]
        assert command[-2:] == ["-@", "-"]
        assert run.call_args.kwargs["input"] == "/a.jpg\n/b.jpg"

    def test_chunks_large_file_lists(self, mocker) -> None:
        completed = MagicMock(stdout="[]")
        mocker.patch("story_book.exif.exiftool_available", return_value=True)
        run = mocker.patch("story_book.exif.subprocess.run", return_value=completed)
        paths = [Path(f"/{i}.jpg") for i in range(5)]

        run_exiftool(paths, chunk_size=2)

        assert run.call_count == 3

    def test_no_paths_skips_subprocess_entirely(self, mocker) -> None:
        run = mocker.patch("story_book.exif.subprocess.run")
        mocker.patch("story_book.exif.exiftool_available", return_value=True)

        assert run_exiftool([]) == {}
        run.assert_not_called()

    def test_returns_empty_when_exiftool_not_installed(self, mocker) -> None:
        mocker.patch("story_book.exif.exiftool_available", return_value=False)
        run = mocker.patch("story_book.exif.subprocess.run")

        assert run_exiftool([Path("/a.jpg")]) == {}
        run.assert_not_called()

    def test_malformed_json_output_degrades_to_empty_rather_than_raising(self, mocker) -> None:
        completed = MagicMock(stdout="not json")
        mocker.patch("story_book.exif.exiftool_available", return_value=True)
        mocker.patch("story_book.exif.subprocess.run", return_value=completed)

        assert run_exiftool([Path("/a.jpg")]) == {}
