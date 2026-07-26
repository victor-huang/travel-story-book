from __future__ import annotations

from pathlib import Path

import pytest

from story_book.config import Config
from story_book.pipeline.base import (
    TRIP_SENTINEL,
    Executor,
    PerItemStage,
    SkipItem,
    StageContext,
)
from story_book.pipeline.runner import worker_count


class TestStageContext:
    def test_cache_dir_lives_under_the_output_dir(self, tmp_path: Path) -> None:
        ctx = StageContext(
            conn=None, config=Config(), out_dir=tmp_path, source_dir=tmp_path / "src"
        )
        assert ctx.cache_dir == tmp_path / ".cache"

    def test_cache_dir_is_created_on_access(self, tmp_path: Path) -> None:
        ctx = StageContext(
            conn=None, config=Config(), out_dir=tmp_path, source_dir=tmp_path / "src"
        )
        assert ctx.cache_dir.is_dir()

    def test_cloud_is_enabled_by_default(self, tmp_path: Path) -> None:
        ctx = StageContext(
            conn=None, config=Config(), out_dir=tmp_path, source_dir=tmp_path / "src"
        )
        assert ctx.no_cloud is False


class TestStageDefaults:
    def test_a_stage_is_available_by_default(self, tmp_path: Path) -> None:
        class Simple(PerItemStage):
            name = "simple"

            def select(self, ctx):
                return []

            def compute(self, media, config):
                return None

            def persist(self, ctx, media, payload):
                pass

        ctx = StageContext(
            conn=None, config=Config(), out_dir=tmp_path, source_dir=tmp_path / "src"
        )
        assert Simple().available(ctx) == (True, "")

    def test_default_executor_is_serial(self) -> None:
        assert PerItemStage.executor is Executor.SERIAL

    def test_default_version_is_one(self) -> None:
        assert PerItemStage.version == 1

    def test_abstract_methods_cannot_be_skipped(self) -> None:
        class Incomplete(PerItemStage):
            name = "incomplete"

        with pytest.raises(TypeError):
            Incomplete()


class TestSkipItem:
    def test_it_carries_a_reason(self) -> None:
        assert str(SkipItem("not a video")) == "not a video"

    def test_it_is_an_exception(self) -> None:
        assert issubclass(SkipItem, Exception)


class TestSentinel:
    def test_the_trip_sentinel_cannot_collide_with_a_hex_hash(self) -> None:
        assert not all(character in "0123456789abcdef" for character in TRIP_SENTINEL)


class TestWorkerCount:
    def test_it_leaves_headroom(self, mocker) -> None:
        mocker.patch("story_book.pipeline.runner.os.cpu_count", return_value=10)
        assert worker_count() == 8

    def test_it_never_returns_zero(self, mocker) -> None:
        mocker.patch("story_book.pipeline.runner.os.cpu_count", return_value=1)
        assert worker_count() == 1

    def test_it_handles_an_unknown_cpu_count(self, mocker) -> None:
        mocker.patch("story_book.pipeline.runner.os.cpu_count", return_value=None)
        assert worker_count() == 1
