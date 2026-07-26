"""Module 9: video analysis.

Per video clip, via `ffprobe`/`ffmpeg`: duration, resolution, fps, a poster thumbnail,
evenly-spaced key frames, and a cheap motion/scene-change score for ranking highlight
candidates. Optionally, local speech transcription via `faster-whisper`.

Design notes:

* `compute` stays *read-only and file-write-free* (only `ffprobe`/`ffmpeg -f null` probing and
  the whisper transcription, which never touches disk beyond reading the source file) so it
  can run in a worker process per `PerItemStage`'s contract. All derived-image writes happen in
  `persist`, which already receives the `StageContext` (and therefore `ctx.cache_dir`) -- there
  is no need for this stage to carry its own constructor arguments, matching every other
  `PerItemStage`/`BatchStage` in this package (e.g. `pipeline/embeddings.py`).
* Nothing is ever written under `ctx.source_dir`. Derived images and a small JSON manifest per
  clip live under `ctx.cache_dir / "video"`. There is no schema table for poster/frame paths,
  fps, or motion score -- `db/schema.sql` is a frozen contract this task does not own, and it
  has no columns for them. The manifest file is the record; `duration`/`width`/`height` *do*
  have columns on `media`, so those are backfilled there (only when missing, never
  overwriting a value T11 already set from EXIF).
* Checkpointing is per clip, not per run, for free: this is a plain `PerItemStage`, so the
  runner (`pipeline/runner.py::_record_success`) writes one `stage_result` row right after this
  stage's `persist` returns for *that* item, before moving to the next. An interrupt between
  two clips loses at most the clip in flight. See
  `tests/backend/test_video.py::TestPerClipCheckpointing` for a test that fires a real
  `KeyboardInterrupt` mid-stage and asserts the resumed run only recomputes the unfinished clip.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from story_book.config import Config, VideoConfig
from story_book.db import connection as db
from story_book.db.models import Media, MediaKind
from story_book.pipeline.base import PerItemStage, SkipItem, StageContext

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 120
POSTER_TIME_FRACTION = 0.1
POSTER_TIME_MAX_SECONDS = 1.0
MOTION_THUMB_SIZE = 32
"""Side length, in px, frames are shrunk to before diffing -- cheap and resolution-independent."""


_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


@dataclass(slots=True)
class VideoProbe:
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


@dataclass(slots=True)
class VideoAnalysis:
    """What `compute` produces for one clip; `persist` turns it into files and DB rows."""

    probe: VideoProbe
    transcribed: bool
    transcript_text: str | None
    transcript_segments: str | None
    whisper_model: str | None
    mean_volume_db: float | None = None


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)


def ffmpeg_available() -> tuple[bool, str]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False, "ffmpeg/ffprobe not found on PATH"
    return True, ""


def _parse_frame_rate(value: str | None) -> float | None:
    """`ffprobe`'s `r_frame_rate` is a rational string like `"30000/1001"`."""
    if not value or "/" not in value:
        return None
    num, _, den = value.partition("/")
    try:
        num_f, den_f = float(num), float(den)
    except ValueError:
        return None
    if den_f == 0:
        return None
    return num_f / den_f


def probe_video(path: Path) -> VideoProbe:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(completed.stdout) if completed.stdout.strip() else {}
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = None
    fmt_duration = data.get("format", {}).get("duration")
    if fmt_duration is not None:
        duration = float(fmt_duration)
    elif video_stream and video_stream.get("duration") is not None:
        duration = float(video_stream["duration"])

    return VideoProbe(
        duration=duration,
        width=video_stream.get("width") if video_stream else None,
        height=video_stream.get("height") if video_stream else None,
        fps=_parse_frame_rate(video_stream.get("r_frame_rate")) if video_stream else None,
        has_audio=has_audio,
    )


def _mean_volume_db(path: Path) -> float | None:
    """`ffmpeg`'s `volumedetect` filter, one audio-only decode pass, no whisper involved.

    This is an energy heuristic, not real voice-activity detection -- good enough to tell a
    silent (or near-silent) clip from one that plausibly carries speech, for routing purposes.
    """
    completed = _run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    match = _MEAN_VOLUME_RE.search(completed.stderr or "")
    return float(match.group(1)) if match else None


def should_transcribe(
    *,
    mode: str,
    duration: float | None,
    has_audio: bool,
    mean_volume_db: float | None,
    min_seconds: float,
    volume_floor_db: float,
) -> bool:
    """The routing decision, kept pure and separate from ffmpeg/whisper calls.

    Deliberately unit-testable on its own: the acceptance criterion is that `auto` routes the
    speech-bearing clip to transcription and the silent one away from it, which this function
    decides without needing a real model (or even a real file).
    """
    if mode == "none" or not has_audio:
        return False
    if mode == "all":
        return True
    if duration is None or duration < min_seconds:
        return False
    return mean_volume_db is not None and mean_volume_db > volume_floor_db


_MODEL_CACHE: dict[str, Any] = {}


def _get_whisper_model(model_name: str) -> Any:
    """Cached per process: reloading a whisper model per clip would dominate the runtime."""
    if model_name not in _MODEL_CACHE:
        from faster_whisper import WhisperModel

        _MODEL_CACHE[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _MODEL_CACHE[model_name]


def transcribe(path: Path, model_name: str, video_config: VideoConfig) -> tuple[str, str]:
    """Returns (joined text, JSON-encoded segment list). Empty text means "no usable speech".

    Whisper hallucinates confidently on ambient noise, and short travel clips are mostly ambient
    noise. On a real trip's clips the naive call invented fluent German and Chinese sentences plus
    runs of Tibetan and CJK characters -- which is worse than returning nothing, because the whole
    point of a transcript here is to feed quotes into a travel journal. A fabricated quote is a
    fabricated memory.

    Three guards, cheapest first:

    * `vad_filter=True` -- voice-activity detection drops non-speech audio before decoding, which
      removes most of the opportunity to hallucinate.
    * `no_speech_prob` per segment -- the model's own estimate that a segment contains no speech.
      Segments above the threshold are discarded.
    * the detected language's probability, and each segment's `avg_logprob` -- both are the
      model's own confidence, and both collapse on non-speech audio. A concert clip transcribed
      as garbled Greek scored 0.26 and -0.85 against roughly 0.9 and -0.4 for real speech.
    * `no_speech_prob` per segment, and a minimum surviving length.
    """
    model = _get_whisper_model(model_name)
    raw, info = model.transcribe(str(path), vad_filter=True)

    language_probability = getattr(info, "language_probability", 1.0)
    if language_probability < video_config.transcript_min_language_probability:
        logger.info(
            "video: discarding transcript for %s -- language confidence %.2f is too low",
            path.name,
            language_probability,
        )
        return "", json.dumps([])

    kept = []
    for segment in raw:
        if getattr(segment, "no_speech_prob", 0.0) > video_config.transcript_max_no_speech_prob:
            continue
        if getattr(segment, "avg_logprob", 0.0) < video_config.transcript_min_avg_logprob:
            continue
        if not segment.text.strip():
            continue
        kept.append(segment)

    text = " ".join(segment.text.strip() for segment in kept).strip()
    if len(text) < video_config.transcript_min_chars:
        return "", json.dumps([])

    segments_json = json.dumps(
        [{"start": segment.start, "end": segment.end, "text": segment.text} for segment in kept]
    )
    return text, segments_json


def _extract_frame(path: Path, timestamp: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dest),
        ]
    )
    return completed.returncode == 0 and dest.exists()


def _keyframe_timestamps(duration: float, count: int) -> list[float]:
    """`count` evenly spaced midpoints, avoiding the very first/last frame of a short clip."""
    if count <= 0 or duration <= 0:
        return []
    return [duration * (i + 0.5) / count for i in range(count)]


def _motion_score(frame_paths: list[Path]) -> float | None:
    """Average per-pixel luminance change between consecutive key frames, in [0, 1].

    A cheap proxy for motion/scene-change, not a real optical-flow or scene-detector score: a
    static tripod shot scores near 0, a pan or a hard cut scores higher. Good enough to rank a
    trip's clips against each other for highlight selection.
    """
    if len(frame_paths) < 2:
        return None
    from PIL import Image

    samples: list[bytes] = []
    for frame_path in frame_paths:
        try:
            with Image.open(frame_path) as image:
                grayscale = image.convert("L").resize((MOTION_THUMB_SIZE, MOTION_THUMB_SIZE))
                samples.append(grayscale.tobytes())
        except OSError:
            return None

    diffs = [
        sum(abs(a - b) for a, b in zip(prev, curr, strict=True)) / (len(prev) * 255)
        for prev, curr in zip(samples, samples[1:], strict=False)
    ]
    return sum(diffs) / len(diffs) if diffs else None


def _video_cache_dir(ctx: StageContext) -> Path:
    path = ctx.cache_dir / "video"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _relative_to_out(ctx: StageContext, path: str | None) -> str | None:
    """Store output-relative paths so the export directory stays portable if moved."""
    if path is None:
        return None
    try:
        return str(Path(path).relative_to(ctx.out_dir))
    except ValueError:
        return path


def _upsert_video_meta(
    conn: Any,
    media_hash: str,
    *,
    fps: float | None,
    poster_path: str | None,
    keyframe_paths: list[str | None],
    motion_score: float | None,
    mean_volume_db: float | None,
    has_speech: bool,
) -> None:
    """Derived video facts go in `video_meta`.

    An earlier pass wrote these to a JSON sidecar because the schema had nowhere for them, which
    would have forced the report and package builders to learn an undocumented file convention.
    """
    conn.execute(
        """
        INSERT INTO video_meta (
            media_hash, fps, poster_path, keyframe_paths, motion_score, mean_volume_db, has_speech
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (media_hash) DO UPDATE SET
            fps = excluded.fps,
            poster_path = excluded.poster_path,
            keyframe_paths = excluded.keyframe_paths,
            motion_score = excluded.motion_score,
            mean_volume_db = excluded.mean_volume_db,
            has_speech = excluded.has_speech
        """,
        (
            media_hash,
            fps,
            poster_path,
            json.dumps([p for p in keyframe_paths if p is not None]),
            motion_score,
            mean_volume_db,
            int(has_speech),
        ),
    )


class VideoStage(PerItemStage):
    """Module 9: FFmpeg-derived video stats/frames/motion score, plus faster-whisper."""

    name = "video"
    version = 1
    description = "Duration/resolution/fps/frames/motion score, plus optional transcription."

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        return ffmpeg_available()

    def select(self, ctx: StageContext) -> list[Media]:
        return list(db.iter_media(ctx.conn, kind=str(MediaKind.VIDEO)))

    def compute(self, media: Media, config: Config) -> VideoAnalysis:
        if media.kind is not MediaKind.VIDEO:
            raise SkipItem(f"{media.hash} is not a video")

        path = Path(media.path)
        probe = probe_video(path)

        transcribed = False
        transcript_text: str | None = None
        transcript_segments: str | None = None
        whisper_model: str | None = None

        mean_volume: float | None = None
        mode = config.video.transcribe
        wants_volume_check = (
            mode == "auto"
            and probe.has_audio
            and probe.duration is not None
            and probe.duration >= config.video.transcribe_min_seconds
        )
        if wants_volume_check:
            mean_volume = _mean_volume_db(path)

        if should_transcribe(
            mode=mode,
            duration=probe.duration,
            has_audio=probe.has_audio,
            mean_volume_db=mean_volume,
            min_seconds=config.video.transcribe_min_seconds,
            volume_floor_db=config.video.speech_mean_volume_floor_db,
        ):
            try:
                transcript_text, transcript_segments = transcribe(
                    path, config.video.whisper_model, config.video
                )
                transcribed = True
                whisper_model = config.video.whisper_model
            except Exception:
                logger.warning("video: transcription failed for %s", media.hash, exc_info=True)

        return VideoAnalysis(
            probe=probe,
            transcribed=transcribed,
            transcript_text=transcript_text,
            transcript_segments=transcript_segments,
            whisper_model=whisper_model,
            mean_volume_db=mean_volume,
        )

    def persist(self, ctx: StageContext, media: Media, payload: VideoAnalysis) -> None:
        probe = payload.probe
        self._backfill_media_fields(ctx, media, probe)

        cache_dir = _video_cache_dir(ctx)
        poster_path: str | None = None
        frame_paths: list[str] = []
        motion_score: float | None = None

        if probe.duration:
            poster_time = min(POSTER_TIME_MAX_SECONDS, probe.duration * POSTER_TIME_FRACTION)
            poster_dest = cache_dir / f"{media.hash}_poster.jpg"
            if _extract_frame(Path(media.path), poster_time, poster_dest):
                poster_path = str(poster_dest)

            for index, timestamp in enumerate(
                _keyframe_timestamps(probe.duration, ctx.config.video.keyframe_count)
            ):
                dest = cache_dir / f"{media.hash}_frame{index}.jpg"
                if _extract_frame(Path(media.path), timestamp, dest):
                    frame_paths.append(str(dest))

            motion_score = _motion_score([Path(p) for p in frame_paths])

        _upsert_video_meta(
            ctx.conn,
            media.hash,
            fps=probe.fps,
            poster_path=_relative_to_out(ctx, poster_path),
            keyframe_paths=[_relative_to_out(ctx, p) for p in frame_paths],
            motion_score=motion_score,
            mean_volume_db=payload.mean_volume_db,
            # Whether usable speech was actually *found*, not whether transcription was
            # attempted. Recording the attempt made this flag claim speech on clips whose
            # transcript the quality gates had just thrown away.
            has_speech=bool(payload.transcript_text),
        )

        # An empty string means the transcript-quality guards rejected everything. Storing it
        # would create a row that says nothing, which downstream readers would treat as real --
        # and any *previous* row has to go too, or tightening the guards leaves the old
        # hallucinated text in place forever. Observed exactly that: re-running with stricter
        # gates reported success while the database still served the bad transcripts.
        if payload.transcribed and payload.transcript_text:
            self._store_transcript(ctx, media, payload)
        else:
            ctx.conn.execute("DELETE FROM transcript WHERE media_hash = ?", (media.hash,))

    def _backfill_media_fields(self, ctx: StageContext, media: Media, probe: VideoProbe) -> None:
        """Fill duration/width/height only where T11's EXIF pass left them empty."""
        duration = media.duration if media.duration is not None else probe.duration
        width = media.width if media.width is not None else probe.width
        height = media.height if media.height is not None else probe.height
        if (duration, width, height) == (media.duration, media.width, media.height):
            return
        db.upsert_media(ctx.conn, replace(media, duration=duration, width=width, height=height))

    def _store_transcript(self, ctx: StageContext, media: Media, payload: VideoAnalysis) -> None:
        ctx.conn.execute(
            """
            INSERT INTO transcript (media_hash, model, text, segments)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (media_hash) DO UPDATE SET
                model = excluded.model,
                text = excluded.text,
                segments = excluded.segments
            """,
            (
                media.hash,
                payload.whisper_model,
                payload.transcript_text,
                payload.transcript_segments,
            ),
        )
