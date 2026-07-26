"""Unit tests for the scan stage: no DB, no real filesystem, no network.

These exercise the pure helpers and the branching logic in `ScanStage._process` by mocking
the filesystem and DB calls it touches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from story_book.db.models import MediaKind
from story_book.pipeline.scan import ScanStage, _hash_file, _iter_candidate_paths


class TestHashFile:
    def test_hash_matches_blake2b_of_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "a.bin"
        path.write_bytes(b"hello world")
        assert _hash_file(path) == hashlib.blake2b(b"hello world").hexdigest()

    def test_empty_file_hashes_to_the_empty_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert _hash_file(path) == hashlib.blake2b(b"").hexdigest()

    def test_reads_in_chunks_not_all_at_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 10)
        monkeypatch.setattr("story_book.pipeline.scan.HASH_CHUNK_SIZE", 4)
        digest = _hash_file(path)
        assert digest == hashlib.blake2b(b"x" * 10).hexdigest()


class TestIterCandidatePaths:
    def test_yields_nested_files(self, tmp_path: Path) -> None:
        (tmp_path / "day1").mkdir()
        (tmp_path / "day1" / "a.jpg").write_bytes(b"a")
        (tmp_path / "b.jpg").write_bytes(b"b")
        found = {p.name for p in _iter_candidate_paths(tmp_path)}
        assert found == {"a.jpg", "b.jpg"}

    def test_prunes_dot_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".Trash").mkdir()
        (tmp_path / ".Trash" / "gone.jpg").write_bytes(b"a")
        (tmp_path / "kept.jpg").write_bytes(b"b")
        found = {p.name for p in _iter_candidate_paths(tmp_path)}
        assert found == {"kept.jpg"}

    def test_does_not_follow_symlinked_directories(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "photo.jpg").write_bytes(b"a")
        source = tmp_path / "source"
        source.mkdir()
        (source / "link").symlink_to(real, target_is_directory=True)
        found = list(_iter_candidate_paths(source))
        assert found == []


class TestScanStageProcess:
    """Exercise `_process`'s branches directly with a mocked context and DB."""

    def _ctx(self, source_dir: Path) -> MagicMock:
        ctx = MagicMock()
        ctx.source_dir = source_dir
        return ctx

    def test_ignored_name_is_skipped(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / ".DS_Store"
        path.write_bytes(b"junk")
        ScanStage()._process(self._ctx(tmp_path), path)
        upsert.assert_not_called()

    def test_hidden_file_is_skipped(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / ".hidden.jpg"
        path.write_bytes(b"junk")
        ScanStage()._process(self._ctx(tmp_path), path)
        upsert.assert_not_called()

    def test_file_in_dot_directory_is_skipped(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        (tmp_path / ".trip").mkdir()
        path = tmp_path / ".trip" / "photo.jpg"
        path.write_bytes(b"junk")
        ScanStage()._process(self._ctx(tmp_path), path)
        upsert.assert_not_called()

    def test_non_media_extension_is_skipped(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / "notes.txt"
        path.write_text("hi")
        ScanStage()._process(self._ctx(tmp_path), path)
        upsert.assert_not_called()

    def test_media_file_is_upserted(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"image bytes")
        ScanStage()._process(self._ctx(tmp_path), path)
        upsert.assert_called_once()
        media = upsert.call_args.args[1]
        assert media.kind is MediaKind.IMAGE
        assert media.bytes == len(b"image bytes")
        assert media.hash == hashlib.blake2b(b"image bytes").hexdigest()

    def test_zero_byte_file_is_still_recorded(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / "empty.jpg"
        path.write_bytes(b"")
        ScanStage()._process(self._ctx(tmp_path), path)
        media = upsert.call_args.args[1]
        assert media.bytes == 0

    def test_unreadable_file_is_skipped_without_raising(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        mocker.patch("story_book.pipeline.scan._hash_file", side_effect=OSError("denied"))
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"x")
        ScanStage()._process(self._ctx(tmp_path), path)  # must not raise
        upsert.assert_not_called()

    def test_stat_failure_is_skipped_without_raising(self, tmp_path: Path, mocker) -> None:
        upsert = mocker.patch("story_book.pipeline.scan.upsert_media")
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"x")
        mocker.patch.object(Path, "stat", side_effect=OSError("gone"))
        ScanStage()._process(self._ctx(tmp_path), path)  # must not raise
        upsert.assert_not_called()

    def test_run_walks_source_dir_and_processes_each_file(self, tmp_path: Path, mocker) -> None:
        (tmp_path / "a.jpg").write_bytes(b"a")
        (tmp_path / "b.jpg").write_bytes(b"b")
        process = mocker.patch.object(ScanStage, "_process")
        ScanStage().run(self._ctx(tmp_path))
        assert process.call_count == 2


class TestScanStageIdentity:
    def test_name_is_scan(self) -> None:
        assert ScanStage.name == "scan"

    def test_has_a_version(self) -> None:
        assert isinstance(ScanStage.version, int)
