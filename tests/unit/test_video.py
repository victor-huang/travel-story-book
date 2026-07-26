"""Unit tests for `pipeline/video.py`: no DB, no filesystem, no network.

`subprocess.run` and `faster-whisper` are always mocked here; real ffmpeg/ffprobe/whisper
coverage lives in `tests/backend/test_video.py`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from story_book.config import Config
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import SkipItem, StageContext
from story_book.pipeline.video import (
    VideoAnalysis,
    VideoProbe,
    VideoStage,
    _keyframe_timestamps,
    _motion_score,
    _parse_frame_rate,
    ffmpeg_available,
    probe_video,
    should_transcribe,
)


def _make_ctx(config: Config | None = None) -> StageContext:
    return StageContext(
        conn=MagicMock(), config=config or Config(), out_dir=Path("/out"), source_dir=Path("/src")
    )


def _media(hash_: str = "h1", path: str = "/src/v.mov", kind: MediaKind = MediaKind.VIDEO) -> Media:
    return Media(hash=hash_, path=path, kind=kind, bytes=100, mtime=0.0)


class TestFfmpegAvailable:
    def test_unavailable_when_ffmpeg_missing(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.shutil.which", return_value=None)
        available, reason = ffmpeg_available()
        assert available is False
        assert "ffmpeg" in reason

    def test_available_when_both_binaries_present(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.shutil.which", return_value="/usr/bin/ffmpeg")
        available, reason = ffmpeg_available()
        assert available is True
        assert reason == ""


class TestVideoStageAvailable:
    def test_delegates_to_ffmpeg_available(self, mocker) -> None:
        mocker.patch(
            "story_book.pipeline.video.ffmpeg_available", return_value=(False, "no ffmpeg")
        )
        assert VideoStage().available(_make_ctx()) == (False, "no ffmpeg")


class TestVideoStageSelect:
    def test_asks_for_video_kind_only(self, mocker) -> None:
        iter_media = mocker.patch("story_book.pipeline.video.db.iter_media", return_value=iter([]))
        VideoStage().select(_make_ctx())
        _, kwargs = iter_media.call_args
        assert kwargs["kind"] == str(MediaKind.VIDEO)

    def test_returns_the_media_list(self, mocker) -> None:
        rows = [_media("a"), _media("b")]
        mocker.patch("story_book.pipeline.video.db.iter_media", return_value=iter(rows))
        assert VideoStage().select(_make_ctx()) == rows


class TestParseFrameRate:
    def test_parses_rational_string(self) -> None:
        assert _parse_frame_rate("30000/1001") == pytest.approx(29.97, abs=0.01)

    def test_none_when_missing(self) -> None:
        assert _parse_frame_rate(None) is None

    def test_none_on_zero_denominator(self) -> None:
        assert _parse_frame_rate("30/0") is None

    def test_none_on_garbage(self) -> None:
        assert _parse_frame_rate("nonsense") is None


class TestProbeVideo:
    def test_parses_duration_dimensions_fps_and_audio(self, mocker) -> None:
        completed = MagicMock(
            stdout=(
                '{"format": {"duration": "3.5"}, "streams": ['
                '{"codec_type": "video", "width": 160, "height": 120, "r_frame_rate": "10/1"},'
                '{"codec_type": "audio"}]}'
            )
        )
        mocker.patch("story_book.pipeline.video._run", return_value=completed)

        probe = probe_video(Path("/src/v.mov"))

        assert probe.duration == pytest.approx(3.5)
        assert probe.width == 160
        assert probe.height == 120
        assert probe.fps == pytest.approx(10.0)
        assert probe.has_audio is True

    def test_no_audio_stream_means_has_audio_false(self, mocker) -> None:
        completed = MagicMock(
            stdout='{"format": {"duration": "1.0"}, "streams": [{"codec_type": "video"}]}'
        )
        mocker.patch("story_book.pipeline.video._run", return_value=completed)

        assert probe_video(Path("/src/v.mov")).has_audio is False

    def test_blank_output_produces_all_nones(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video._run", return_value=MagicMock(stdout=""))

        probe = probe_video(Path("/src/v.mov"))

        assert probe.duration is None
        assert probe.width is None
        assert probe.has_audio is False


class TestKeyframeTimestamps:
    def test_evenly_spaces_midpoints(self) -> None:
        timestamps = _keyframe_timestamps(10.0, 5)
        assert timestamps == pytest.approx([1.0, 3.0, 5.0, 7.0, 9.0])

    def test_empty_when_count_is_zero(self) -> None:
        assert _keyframe_timestamps(10.0, 0) == []

    def test_empty_when_duration_is_zero(self) -> None:
        assert _keyframe_timestamps(0.0, 5) == []


class TestMotionScore:
    def test_none_with_fewer_than_two_frames(self) -> None:
        assert _motion_score([Path("/only/one.jpg")]) is None

    def test_higher_for_more_different_frames(self, tmp_path: Path) -> None:
        from PIL import Image

        still_a = tmp_path / "still_a.jpg"
        still_b = tmp_path / "still_b.jpg"
        moving_a = tmp_path / "moving_a.jpg"
        moving_b = tmp_path / "moving_b.jpg"
        Image.new("RGB", (32, 32), (10, 10, 10)).save(still_a)
        Image.new("RGB", (32, 32), (10, 10, 10)).save(still_b)
        Image.new("RGB", (32, 32), (0, 0, 0)).save(moving_a)
        Image.new("RGB", (32, 32), (255, 255, 255)).save(moving_b)

        still_score = _motion_score([still_a, still_b])
        moving_score = _motion_score([moving_a, moving_b])

        assert still_score == pytest.approx(0.0)
        assert moving_score > still_score

    def test_none_when_a_frame_cannot_be_opened(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.jpg"
        also_missing = tmp_path / "also_missing.jpg"
        assert _motion_score([missing, also_missing]) is None


class TestShouldTranscribe:
    def test_none_mode_never_transcribes(self) -> None:
        assert (
            should_transcribe(
                mode="none", duration=100.0, has_audio=True, mean_volume_db=-10.0, min_seconds=1.0
            )
            is False
        )

    def test_no_audio_track_never_transcribes(self) -> None:
        assert (
            should_transcribe(
                mode="all", duration=100.0, has_audio=False, mean_volume_db=None, min_seconds=1.0
            )
            is False
        )

    def test_all_mode_transcribes_regardless_of_duration(self) -> None:
        assert (
            should_transcribe(
                mode="all", duration=0.5, has_audio=True, mean_volume_db=None, min_seconds=10.0
            )
            is True
        )

    def test_auto_mode_skips_short_clips(self) -> None:
        assert (
            should_transcribe(
                mode="auto", duration=5.0, has_audio=True, mean_volume_db=-10.0, min_seconds=10.0
            )
            is False
        )

    def test_auto_mode_skips_quiet_clips(self) -> None:
        assert (
            should_transcribe(
                mode="auto", duration=30.0, has_audio=True, mean_volume_db=-80.0, min_seconds=10.0
            )
            is False
        )

    def test_auto_mode_transcribes_long_clips_with_signal(self) -> None:
        assert (
            should_transcribe(
                mode="auto", duration=30.0, has_audio=True, mean_volume_db=-20.0, min_seconds=10.0
            )
            is True
        )

    def test_auto_mode_treats_unknown_volume_as_silent(self) -> None:
        assert (
            should_transcribe(
                mode="auto", duration=30.0, has_audio=True, mean_volume_db=None, min_seconds=10.0
            )
            is False
        )


class TestVideoStageComputeRoutesToSkip:
    def test_raises_skip_item_for_non_video_media(self) -> None:
        with pytest.raises(SkipItem):
            VideoStage().compute(_media(kind=MediaKind.IMAGE), Config())


class TestVideoStageComputeTranscription:
    def _probe(self, **overrides) -> VideoProbe:
        defaults = dict(duration=30.0, width=100, height=100, fps=30.0, has_audio=True)
        return VideoProbe(**{**defaults, **overrides})

    def test_calls_transcribe_when_routing_says_yes(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.probe_video", return_value=self._probe())
        mocker.patch("story_book.pipeline.video._mean_volume_db", return_value=-10.0)
        transcribe = mocker.patch(
            "story_book.pipeline.video.transcribe", return_value=("hello", "[]")
        )

        payload = VideoStage().compute(_media(), Config())

        transcribe.assert_called_once()
        assert payload.transcribed is True
        assert payload.transcript_text == "hello"

    def test_does_not_call_transcribe_for_silent_clip(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.probe_video", return_value=self._probe())
        mocker.patch("story_book.pipeline.video._mean_volume_db", return_value=-90.0)
        transcribe = mocker.patch("story_book.pipeline.video.transcribe")

        payload = VideoStage().compute(_media(), Config())

        transcribe.assert_not_called()
        assert payload.transcribed is False
        assert payload.transcript_text is None

    def test_transcribe_mode_none_never_checks_volume(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.probe_video", return_value=self._probe())
        mean_volume = mocker.patch("story_book.pipeline.video._mean_volume_db")
        transcribe = mocker.patch("story_book.pipeline.video.transcribe")
        config = replace(Config(), video=replace(Config().video, transcribe="none"))

        payload = VideoStage().compute(_media(), config)

        mean_volume.assert_not_called()
        transcribe.assert_not_called()
        assert payload.transcribed is False

    def test_transcription_failure_is_swallowed_not_raised(self, mocker) -> None:
        mocker.patch("story_book.pipeline.video.probe_video", return_value=self._probe())
        mocker.patch("story_book.pipeline.video._mean_volume_db", return_value=-10.0)
        mocker.patch(
            "story_book.pipeline.video.transcribe", side_effect=RuntimeError("model missing")
        )

        payload = VideoStage().compute(_media(), Config())

        assert payload.transcribed is False
        assert payload.transcript_text is None

    def test_no_audio_track_skips_volume_check_entirely(self, mocker) -> None:
        mocker.patch(
            "story_book.pipeline.video.probe_video", return_value=self._probe(has_audio=False)
        )
        mean_volume = mocker.patch("story_book.pipeline.video._mean_volume_db")

        VideoStage().compute(_media(), Config())

        mean_volume.assert_not_called()


class TestVideoStagePersist:
    """`_video_cache_dir` and `_write_manifest` are always mocked here -- persist's disk
    writes (frames, manifest) are exercised for real in `tests/backend/test_video.py`.
    """

    def _patch_filesystem(self, mocker):
        mocker.patch("story_book.pipeline.video._video_cache_dir", return_value=Path("/fake/cache"))
        mocker.patch("story_book.pipeline.video._write_manifest")
        mocker.patch("story_book.pipeline.video._extract_frame", return_value=False)

    def test_backfills_missing_duration_width_height(self, mocker) -> None:
        self._patch_filesystem(mocker)
        upsert = mocker.patch("story_book.pipeline.video.db.upsert_media")
        media = _media()
        payload = VideoAnalysis(
            probe=VideoProbe(duration=3.0, width=160, height=120, fps=10.0, has_audio=False),
            transcribed=False,
            transcript_text=None,
            transcript_segments=None,
            whisper_model=None,
        )

        VideoStage().persist(_make_ctx(), media, payload)

        (_conn, updated), _kwargs = upsert.call_args
        assert updated.duration == 3.0
        assert updated.width == 160
        assert updated.height == 120

    def test_does_not_overwrite_existing_media_fields(self, mocker) -> None:
        self._patch_filesystem(mocker)
        upsert = mocker.patch("story_book.pipeline.video.db.upsert_media")
        media = _media()
        media.duration = 99.0
        media.width = 4000
        media.height = 3000
        payload = VideoAnalysis(
            probe=VideoProbe(duration=3.0, width=160, height=120, fps=10.0, has_audio=False),
            transcribed=False,
            transcript_text=None,
            transcript_segments=None,
            whisper_model=None,
        )

        VideoStage().persist(_make_ctx(), media, payload)

        upsert.assert_not_called()

    def test_writes_transcript_row_when_transcribed(self, mocker) -> None:
        self._patch_filesystem(mocker)
        mocker.patch("story_book.pipeline.video.db.upsert_media")
        media = _media()
        media.duration = 3.0
        media.width = 1
        media.height = 1
        payload = VideoAnalysis(
            probe=VideoProbe(duration=3.0, width=1, height=1, fps=10.0, has_audio=True),
            transcribed=True,
            transcript_text="hello there",
            transcript_segments="[]",
            whisper_model="small",
        )
        ctx = _make_ctx()

        VideoStage().persist(ctx, media, payload)

        sql, params = ctx.conn.execute.call_args_list[-1].args
        assert "INSERT INTO transcript" in sql
        assert params == (media.hash, "small", "hello there", "[]")

    def test_no_transcript_row_when_not_transcribed(self, mocker) -> None:
        self._patch_filesystem(mocker)
        mocker.patch("story_book.pipeline.video.db.upsert_media")
        media = _media()
        media.duration = 3.0
        media.width = 1
        media.height = 1
        payload = VideoAnalysis(
            probe=VideoProbe(duration=3.0, width=1, height=1, fps=10.0, has_audio=False),
            transcribed=False,
            transcript_text=None,
            transcript_segments=None,
            whisper_model=None,
        )
        ctx = _make_ctx()

        VideoStage().persist(ctx, media, payload)

        transcript_calls = [
            c for c in ctx.conn.execute.call_args_list if "INSERT INTO transcript" in c.args[0]
        ]
        assert transcript_calls == []
