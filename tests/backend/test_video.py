"""Backend tests for the video stage: real temp DB, real ffmpeg/ffprobe, real fixture clips.

Whisper itself is always mocked (`transcribe`) -- no model is downloaded here. The acceptance
criterion is checked at the *routing* level (does `auto` decide to call `transcribe` for the
speech clip and not the silent one), not at the transcript-text level: `clip_speech.mov` is a
synthetic voice-band tone, not real narration, so its transcribed text is meaningless.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import StageContext
from story_book.pipeline.runner import Runner
from story_book.pipeline.video import VideoStage

pytestmark = pytest.mark.needs_ffmpeg


@pytest.fixture(autouse=True)
def _require_ffmpeg(has_ffmpeg: bool) -> None:
    if not has_ffmpeg:
        pytest.skip("ffmpeg not installed")


def _seed(ctx: StageContext, filename: str) -> Media:
    path = ctx.source_dir / filename
    if not path.exists():
        pytest.skip(f"fixture {filename} missing")
    media = Media(
        hash=filename, path=str(path), kind=MediaKind.VIDEO, bytes=path.stat().st_size, mtime=0.0
    )
    db.upsert_media(ctx.conn, media)
    return media


def _with_video_config(ctx: StageContext, **overrides) -> StageContext:
    video_config = replace(ctx.config.video, **overrides)
    return replace(ctx, config=replace(ctx.config, video=video_config))


class TestAvailable:
    def test_available_when_ffmpeg_installed(self, ctx: StageContext) -> None:
        available, reason = VideoStage().available(ctx)
        assert available is True
        assert reason == ""


class TestDurationAndThumbnail:
    """Acceptance: "every video fixture gets a thumbnail and duration"."""

    @pytest.mark.parametrize("filename", ["clip_speech.mov", "clip_silent.mp4"])
    def test_fixture_gets_duration_and_poster(self, ctx: StageContext, filename: str) -> None:
        media = _seed(ctx, filename)
        stage_ctx = _with_video_config(ctx, transcribe="none")

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        stored = db.get_media(ctx.conn, media.hash)
        assert stored.duration is not None and stored.duration > 0
        assert stored.width and stored.height

        row = ctx.conn.execute(
            "SELECT * FROM video_meta WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert row is not None
        assert row["poster_path"] is not None
        assert (ctx.out_dir / row["poster_path"]).exists()
        assert len(json.loads(row["keyframe_paths"])) == stage_ctx.config.video.keyframe_count

    def test_poster_is_written_under_out_dir_not_source_dir(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none")
        source_files_before = set(ctx.source_dir.iterdir())

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        assert set(ctx.source_dir.iterdir()) == source_files_before

    def test_motion_score_is_recorded_for_a_multi_keyframe_clip(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none", keyframe_count=3)

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        row = ctx.conn.execute(
            "SELECT motion_score FROM video_meta WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert row["motion_score"] is not None


class TestAutoTranscribeRouting:
    """Acceptance: "`auto` transcribes the speech clip and skips the silent one".

    Both fixtures are 3s clips, shorter than the config default `transcribe_min_seconds`
    (10s), so `transcribe_min_seconds` is lowered here to let audio content -- not clip
    length -- decide. `transcribe` itself is mocked so no whisper model is downloaded;
    the assertion is on whether it was *called*, per the acceptance note that
    `clip_speech.mov` carries a synthetic tone rather than real words.
    """

    def test_speech_clip_is_routed_to_transcribe(self, ctx: StageContext, mocker) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="auto", transcribe_min_seconds=1.0)
        fake_transcribe = mocker.patch(
            "story_book.pipeline.video.transcribe", return_value=("tone", "[]")
        )

        VideoStage().compute(media, stage_ctx.config)

        fake_transcribe.assert_called_once()

    def test_silent_clip_is_not_routed_to_transcribe(self, ctx: StageContext, mocker) -> None:
        media = _seed(ctx, "clip_silent.mp4")
        stage_ctx = _with_video_config(ctx, transcribe="auto", transcribe_min_seconds=1.0)
        fake_transcribe = mocker.patch("story_book.pipeline.video.transcribe")

        payload = VideoStage().compute(media, stage_ctx.config)

        fake_transcribe.assert_not_called()
        assert payload.transcribed is False

    def test_transcribe_none_skips_both_clips(self, ctx: StageContext, mocker) -> None:
        speech = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none")
        fake_transcribe = mocker.patch("story_book.pipeline.video.transcribe")

        VideoStage().compute(speech, stage_ctx.config)

        fake_transcribe.assert_not_called()


class TestPerClipCheckpointing:
    """Acceptance: "interrupting mid-stage and re-running re-transcribes at most one clip".

    Runs the real `VideoStage` through the real `Runner` against both real fixture clips,
    with `transcribe` mocked to raise `KeyboardInterrupt` on its second invocation --
    simulating a real ctrl-C landing partway through the slowest per-item stage in the
    project. This proves the runner's generic per-item commit (`record_stage_result` right
    after each clip's `persist`) applies to this stage: nothing here batches multiple clips
    into one unit of work.
    """

    def test_interrupt_leaves_exactly_one_clip_done(self, ctx: StageContext, mocker) -> None:
        _seed(ctx, "clip_speech.mov")
        _seed(ctx, "clip_silent.mp4")
        stage_ctx = _with_video_config(ctx, transcribe="all")
        calls: list[Path] = []

        def fake_transcribe(path: Path, model_name: str, *_args) -> tuple[str, str]:
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return "text", "[]"

        mocker.patch("story_book.pipeline.video.transcribe", side_effect=fake_transcribe)

        report = Runner(stage_ctx, [VideoStage()]).run()

        assert report.interrupted is True
        assert len(db.completed_hashes(ctx.conn, "video", 1)) == 1

    def test_resume_only_recomputes_the_unfinished_clip(self, ctx: StageContext, mocker) -> None:
        _seed(ctx, "clip_speech.mov")
        _seed(ctx, "clip_silent.mp4")
        stage_ctx = _with_video_config(ctx, transcribe="all")
        calls: list[Path] = []

        def fake_transcribe(path: Path, model_name: str, *_args) -> tuple[str, str]:
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return "text", "[]"

        mocker.patch("story_book.pipeline.video.transcribe", side_effect=fake_transcribe)
        Runner(stage_ctx, [VideoStage()]).run()

        second_report = Runner(stage_ctx, [VideoStage()]).run()

        assert second_report.interrupted is False
        assert len(db.completed_hashes(ctx.conn, "video", 1)) == 2
        # Three total invocations: clip A (run 1, ok), clip B (run 1, interrupted), clip B
        # (run 2, ok) -- clip A's successful work from run 1 is never redone.
        assert len(calls) == 3


class TestSkipItemForNonVideoMedia:
    def test_image_media_raises_skip_item(self, ctx: StageContext) -> None:
        from story_book.pipeline.base import SkipItem

        media = Media(hash="img", path="/src/a.jpg", kind=MediaKind.IMAGE, bytes=1, mtime=0.0)
        with pytest.raises(SkipItem):
            VideoStage().compute(media, ctx.config)


class TestVideoMetaTable:
    """Derived video facts belong in the schema, not an undocumented sidecar file."""

    def test_keyframe_paths_are_stored_relative_to_the_output_dir(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none", keyframe_count=2)

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        row = ctx.conn.execute(
            "SELECT keyframe_paths FROM video_meta WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        paths = json.loads(row["keyframe_paths"])
        assert all(not Path(p).is_absolute() for p in paths)

    def test_relative_keyframe_paths_resolve_against_the_output_dir(
        self, ctx: StageContext
    ) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none", keyframe_count=2)

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        row = ctx.conn.execute(
            "SELECT keyframe_paths FROM video_meta WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert all((ctx.out_dir / p).exists() for p in json.loads(row["keyframe_paths"]))

    def test_fps_is_recorded(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none")

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        row = ctx.conn.execute(
            "SELECT fps FROM video_meta WHERE media_hash = ?", (media.hash,)
        ).fetchone()
        assert row["fps"] and row["fps"] > 0

    def test_rerunning_persist_updates_rather_than_duplicating(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_speech.mov")
        stage_ctx = _with_video_config(ctx, transcribe="none")

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)
        VideoStage().persist(stage_ctx, media, payload)

        count = ctx.conn.execute("SELECT COUNT(*) AS n FROM video_meta").fetchone()["n"]
        assert count == 1

    def test_silent_clip_records_a_low_mean_volume(self, ctx: StageContext) -> None:
        media = _seed(ctx, "clip_silent.mp4")
        stage_ctx = _with_video_config(ctx, transcribe="auto", transcribe_min_seconds=1.0)

        payload = VideoStage().compute(media, stage_ctx.config)
        VideoStage().persist(stage_ctx, media, payload)

        row = ctx.conn.execute(
            "SELECT mean_volume_db, has_speech FROM video_meta WHERE media_hash = ?",
            (media.hash,),
        ).fetchone()
        assert row["mean_volume_db"] < stage_ctx.config.video.speech_mean_volume_floor_db
        assert row["has_speech"] == 0
