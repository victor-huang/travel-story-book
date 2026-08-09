"""Integration tests for the reel: real ffmpeg, real fixture media, real bytes on disk.

The lessons these encode:

* **P06** -- nine assets declared `kind: "video"` whose exported files were JPEGs under `.mov`
  names. Every presence test passed. So here the container is checked with `file`-style magic
  bytes and a clip is checked for *actual motion*, not merely for existing.
* **T43** -- a resume test reported a pass three times without interrupting anything, because the
  exit code was the only observation. So resume is asserted by counting what got recomputed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

from story_book.config import Config, ReelConfig
from story_book.export.reel import (
    REEL_JSON_FILENAME,
    SEGMENT_CACHE_DIRNAME,
    ClipSource,
    ReelError,
    _segment_offsets,
    build_plan,
    frame_size,
    render_reel,
    render_title_card,
    resolve_clip_sources,
    segment_key,
)
from story_book.export.subtitles import cue_font_size

pytestmark = pytest.mark.needs_ffmpeg

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "media"


def _fast_config(**reel_overrides) -> Config:
    """Small and short: these tests are about correctness, not encoder quality."""
    defaults = {
        "height": 240,
        "seconds_per_still": 1.0,
        "seconds_per_title": 1.0,
        "crossfade_seconds": 0.3,
        "clip_seconds": 2.0,
        "x264_preset": "ultrafast",
    }
    defaults.update(reel_overrides)
    return Config(reel=ReelConfig(**defaults))


@pytest.fixture
def trip(tmp_path: Path) -> dict:
    """A trip.json-shaped document backed by real fixture images copied into previews/."""
    previews = tmp_path / "previews"
    previews.mkdir()
    assets = {}
    for index, name in enumerate(["sharp.jpg", "distinct_a.jpg", "distinct_b.jpg"]):
        asset_id = f"asset{index}"
        shutil.copy(FIXTURES / name, previews / f"{asset_id}.jpg")
        assets[asset_id] = {
            "asset_id": asset_id,
            "filename": name,
            "kind": "image",
            "taken_utc": f"2026-07-18T09:0{index}:00+00:00",
            "day": "2026-07-18",
            "preview": f"previews/{asset_id}.jpg",
            "thumbnail": f"previews/{asset_id}.jpg",
            "location": {"place": {"city": "Vienna"}},
        }
    return {
        "trip": {"name": "Fixture Trip", "start_local": "2026-07-18", "end_local": "2026-07-18"},
        "assets": assets,
        "days": [
            {
                "date": "2026-07-18",
                "highlights": list(assets),
                "events": [{"id": "2026-07-18#1", "assets": list(assets)}],
            }
        ],
    }


def _magic(path: Path) -> bytes:
    return path.read_bytes()[:12]


def _is_mp4(path: Path) -> bool:
    """`ftyp` at offset 4 is the ISO base media signature -- what `file -b` reads."""
    return _magic(path)[4:8] == b"ftyp"


def _probe(path: Path, entries: str) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _frame_change(path: Path, width: int = 32, height: int = 18) -> float:
    """Mean absolute difference between consecutive frames. A repeated still scores ~0."""
    dump = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"fps=8,scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    )  # fmt: skip
    size = width * height
    frames = [dump.stdout[i * size : (i + 1) * size] for i in range(len(dump.stdout) // size)]
    if len(frames) < 2:
        return 0.0
    diffs = [sum(abs(a - b) for a, b in zip(x, y, strict=True)) / size for x, y in pairwise(frames)]
    return max(diffs)


def _loudness(path: Path, start: float, end: float, band: int | None = None) -> float:
    """Mean volume in dB over `[start, end)`, optionally isolated to one frequency band.

    The band matters: measuring the whole spectrum cannot tell a ducked music bed from the clip
    audio playing over it, so a ducking assertion made that way would pass on the wrong evidence.
    """
    chain = "volume=1.0" if band is None else f"bandpass=f={band}:width_type=h:w=40"
    # No `-v error`: volumedetect reports at info level, and silencing it makes every
    # measurement come back as the "no reading" sentinel, which compares equal to itself.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-i", str(path), "-af", f"{chain},volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    return float(match.group(1)) if match else -999.0


def _alpha_fraction(png: Path) -> float:
    """Fraction of a transparent cue image that has any ink in it."""
    from PIL import Image

    with Image.open(png) as image:
        histogram = image.split()[-1].histogram()
    total = sum(histogram)
    return sum(histogram[1:]) / total if total else 0.0


def _drawn_pixel_fraction(png: Path) -> float:
    """Fraction of pixels brighter than the card background -- i.e. how much text got drawn."""
    from PIL import Image

    with Image.open(png) as image:
        histogram = image.convert("L").histogram()
    total = sum(histogram)
    return sum(histogram[91:]) / total if total else 0.0


class TestFixturesArePresent:
    def test_the_media_fixtures_this_module_needs_exist(self):
        for name in [
            "sharp.jpg",
            "distinct_a.jpg",
            "distinct_b.jpg",
            "clip_silent.mp4",
            "clip_speech.mov",
        ]:
            assert (FIXTURES / name).exists(), f"committed fixture missing: {name}"


class TestRenderedFile:
    def test_produces_a_playable_mp4(self, trip, tmp_path):
        plan = build_plan(trip, _fast_config())
        rendered = render_reel(plan, _fast_config(), tmp_path)
        assert rendered.path.exists()

    def test_the_bytes_are_actually_an_mp4_not_something_renamed(self, trip, tmp_path):
        """P06: nine 'videos' in an export turned out to be JPEGs under .mov names."""
        rendered = render_reel(build_plan(trip, _fast_config()), _fast_config(), tmp_path)
        assert _is_mp4(rendered.path)

    def test_the_video_stream_is_h264(self, trip, tmp_path):
        rendered = render_reel(build_plan(trip, _fast_config()), _fast_config(), tmp_path)
        assert _probe(rendered.path, "stream=codec_name").splitlines()[0] == "h264"

    def test_the_frame_is_the_configured_aspect(self, trip, tmp_path):
        config = _fast_config(aspect="16:9")
        rendered = render_reel(build_plan(trip, config), config, tmp_path)
        width, height = frame_size(config)
        assert _probe(rendered.path, "stream=width,height").split() == [str(width), str(height)]

    def test_dimensions_are_rounded_up_to_stay_even(self):
        """16:9 at 240 is 426.67, and H.264 with yuv420p cannot have an odd axis."""
        assert frame_size(_fast_config(aspect="16:9")) == (428, 240)

    def test_a_vertical_aspect_renders_vertical(self, trip, tmp_path):
        config = _fast_config(aspect="9:16")
        rendered = render_reel(build_plan(trip, config), config, tmp_path)
        width, height = _probe(rendered.path, "stream=width,height").split()
        assert int(height) > int(width)

    def test_the_duration_matches_the_plan(self, trip, tmp_path):
        config = _fast_config()
        plan = build_plan(trip, config)
        rendered = render_reel(plan, config, tmp_path)
        assert float(_probe(rendered.path, "format=duration")) == pytest.approx(
            plan.duration, abs=0.5
        )

    def test_there_is_no_audio_stream_without_music(self, trip, tmp_path):
        rendered = render_reel(build_plan(trip, _fast_config()), _fast_config(), tmp_path)
        assert "audio" not in _probe(rendered.path, "stream=codec_type")


class TestMotion:
    def test_a_still_only_reel_holds_still(self, trip, tmp_path):
        """The control for the test below: without clips, only crossfades change pixels.

        The end card is off here because it is a *different* picture from the last still, so
        cutting to it moves pixels by design -- which is not what this test is about."""
        config = _fast_config(crossfade_seconds=0.0, end_card=False)
        rendered = render_reel(build_plan(trip, config), config, tmp_path)
        assert _frame_change(rendered.path) < 5.0

    def test_a_clip_segment_actually_moves(self, trip, tmp_path):
        """Declaring a clip is not the same as rendering one -- the P06 defect exactly."""
        clip = FIXTURES / "clip_silent.mp4"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_silent.mp4",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": float(_probe(clip, "format=duration"))},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config(crossfade_seconds=0.0)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        assert any(s.kind == "clip" for s in plan.segments)

        rendered = render_reel(plan, config, tmp_path)
        assert _frame_change(rendered.path) > 5.0


class TestSegmentCache:
    def test_a_second_render_recomputes_nothing(self, trip, tmp_path):
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        again = render_reel(build_plan(trip, config), config, tmp_path)
        assert (again.segments_rendered, again.segments_cached) == (0, len(again.plan.segments))

    def test_deleting_one_segment_recomputes_only_that_one(self, trip, tmp_path):
        """Resume, asserted by count. T43's version of this reported three false passes."""
        config = _fast_config()
        first = render_reel(build_plan(trip, config), config, tmp_path)
        cache = tmp_path / "reel" / SEGMENT_CACHE_DIRNAME
        sorted(cache.glob("*.mp4"))[0].unlink()

        again = render_reel(build_plan(trip, config), config, tmp_path)
        assert again.segments_rendered == 1
        assert again.segments_cached == len(first.plan.segments) - 1

    def test_adding_a_photo_reuses_every_existing_segment(self, trip, tmp_path):
        """The reason the key is the spec and not the position in the list.

        The end card is off here because its mosaic samples across the whole reel, so adding a
        photo legitimately changes it -- see the test below, which asserts exactly that."""
        config = _fast_config(end_card=False)
        before = render_reel(build_plan(trip, config), config, tmp_path)
        shutil.copy(FIXTURES / "burst_a.jpg", tmp_path / "previews" / "new.jpg")
        trip["assets"]["new"] = {
            "asset_id": "new",
            "filename": "burst_a.jpg",
            "kind": "image",
            "taken_utc": "2026-07-18T08:00:00+00:00",  # earliest: lands at the front
            "day": "2026-07-18",
            "preview": "previews/new.jpg",
            "thumbnail": "previews/new.jpg",
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["highlights"].append("new")
        trip["days"][0]["events"][0]["assets"].append("new")

        after = render_reel(build_plan(trip, config), config, tmp_path)
        assert after.segments_rendered == 1
        assert after.segments_cached == len(before.plan.segments)

    def test_adding_a_photo_rebuilds_the_end_card(self, trip, tmp_path):
        """The mosaic is sampled across the reel, so new material belongs in it. The cache has to
        see that, which is why the tile list is part of the segment spec."""
        config = _fast_config()
        before = build_plan(trip, config)
        shutil.copy(FIXTURES / "burst_a.jpg", tmp_path / "previews" / "new.jpg")
        trip["assets"]["new"] = {
            "asset_id": "new",
            "filename": "burst_a.jpg",
            "kind": "image",
            "taken_utc": "2026-07-18T08:00:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/new.jpg",
            "thumbnail": "previews/new.jpg",
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["highlights"].append("new")
        trip["days"][0]["events"][0]["assets"].append("new")
        after = build_plan(trip, config)

        assert before.segments[-1].sources != after.segments[-1].sources
        assert segment_key(before.segments[-1], before, config) != segment_key(
            after.segments[-1], after, config
        )

    def test_two_renders_of_an_unchanged_trip_agree_on_every_key(self, trip, tmp_path):
        config = _fast_config()
        first = build_plan(trip, config)
        second = build_plan(trip, config)
        assert [segment_key(s, first, config) for s in first.segments] == [
            segment_key(s, second, config) for s in second.segments
        ]

    def test_a_stale_partial_from_a_killed_run_is_discarded(self, trip, tmp_path):
        """An interrupted render leaves a half-written `.partial.mp4`. It is never valid."""
        config = _fast_config()
        plan = build_plan(trip, config)
        cache = tmp_path / "reel" / SEGMENT_CACHE_DIRNAME
        cache.mkdir(parents=True)
        key = segment_key(plan.segments[0], plan, config)
        stale = cache / f"{key}.partial.mp4"
        stale.write_bytes(b"truncated garbage")

        render_reel(plan, config, tmp_path)

        assert not stale.exists()
        assert _is_mp4(cache / f"{key}.mp4")

    def test_ffmpeg_succeeding_without_output_is_a_clear_error(self, trip, tmp_path, mocker):
        """ffmpeg can exit 0 and write nothing. Checking only the exit code turned that into a
        FileNotFoundError from a rename, three frames from the cause."""
        mocker.patch("story_book.export.reel._run_ffmpeg", return_value=None)
        config = _fast_config()
        with pytest.raises(ReelError, match="produced no output"):
            render_reel(build_plan(trip, config), config, tmp_path)

    def test_an_interrupted_render_leaves_no_valid_looking_cache_entry(self, trip, tmp_path):
        """A half-written segment must not be mistaken for a finished one on the next run."""
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        cache = tmp_path / "reel" / SEGMENT_CACHE_DIRNAME
        assert not list(cache.glob("*.partial.mp4"))


class TestMusic:
    @pytest.fixture
    def track(self, tmp_path: Path) -> Path:
        target = tmp_path / "track.m4a"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:a", "aac", str(target)],
            check=True,
        )  # fmt: skip
        return target

    def test_music_produces_an_audio_stream(self, trip, tmp_path, track):
        config = _fast_config()
        rendered = render_reel(build_plan(trip, config), config, tmp_path, music=track)
        assert "audio" in _probe(rendered.path, "stream=codec_type")

    def test_a_short_track_is_looped_to_cover_the_reel(self, trip, tmp_path, track):
        config = _fast_config()
        plan = build_plan(trip, config)
        rendered = render_reel(plan, config, tmp_path, music=track)
        assert float(_probe(rendered.path, "format=duration")) == pytest.approx(
            plan.duration, abs=0.5
        )


class TestClipAudio:
    """The clip's own sound is usually the reason the clip is in the reel at all."""

    def _with_clip(self, trip: dict, fixture: str) -> Path:
        clip = FIXTURES / fixture
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": fixture,
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": float(_probe(clip, "format=duration"))},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        return clip

    def test_a_clip_with_speech_gives_the_reel_an_audio_stream(self, trip, tmp_path):
        clip = self._with_clip(trip, "clip_speech.mov")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        rendered = render_reel(plan, config, tmp_path)
        assert "audio" in _probe(rendered.path, "stream=codec_type")

    def test_the_clip_is_reported_as_carrying_sound(self, trip, tmp_path):
        clip = self._with_clip(trip, "clip_speech.mov")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        render_reel(plan, config, tmp_path)
        assert plan.clips_with_sound == ["clip_speech.mov"]

    def test_a_clip_with_no_audio_track_is_named_not_silently_ignored(self, trip, tmp_path):
        """Both committed clip fixtures carry an audio stream -- `clip_silent.mp4` is silent
        *content*, not a missing track -- so a genuinely track-less video is made here."""
        mute = tmp_path / "no_audio.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=15:duration=3", "-an", str(mute)],
            check=True,
        )  # fmt: skip
        self._with_clip(trip, "clip_speech.mov")
        trip["assets"]["vid"]["filename"] = "no_audio.mp4"
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", mute)})
        render_reel(plan, config, tmp_path)
        assert plan.clips_without_sound == ["no_audio.mp4"]
        assert plan.clips_with_sound == []

    def test_a_reel_whose_only_clip_has_no_audio_stays_silent(self, trip, tmp_path):
        mute = tmp_path / "no_audio.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=15:duration=3", "-an", str(mute)],
            check=True,
        )  # fmt: skip
        self._with_clip(trip, "clip_speech.mov")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", mute)})
        rendered = render_reel(plan, config, tmp_path)
        assert "audio" not in _probe(rendered.path, "stream=codec_type")

    def test_a_clip_with_a_second_undecodable_audio_stream_still_renders(self, trip, tmp_path):
        """A modern iPhone writes spatial audio as an extra `apac` track this ffmpeg cannot
        decode. Mapping every audio stream failed on 58 of 69 real clips; only the first is
        wanted anyway. Proxies hid it, since transcoding one picks a single stream by default."""
        clip = tmp_path / "two_audio.mov"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
             "-map", "0:v", "-map", "1:a", "-map", "2:a",
             "-c:v", "libx264", "-c:a", "aac", str(clip)],
            check=True,
        )  # fmt: skip
        assert len(_probe(clip, "stream=codec_type").split()) == 3

        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "two_audio.mov",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": 3.0},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        rendered = render_reel(plan, config, tmp_path)

        assert plan.clips_with_sound == ["two_audio.mov"]
        assert _probe(rendered.path, "stream=codec_type").count("audio") == 1

    def test_disabling_clip_audio_leaves_the_reel_silent(self, trip, tmp_path):
        clip = self._with_clip(trip, "clip_speech.mov")
        config = _fast_config(clip_audio=False)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        rendered = render_reel(plan, config, tmp_path)
        assert "audio" not in _probe(rendered.path, "stream=codec_type")

    def test_sound_lands_where_the_clip_does_not_at_the_start(self, trip, tmp_path):
        """Clip audio is delayed by the same accumulation the crossfades use."""
        clip = self._with_clip(trip, "clip_speech.mov")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        rendered = render_reel(plan, config, tmp_path)

        clip_index = next(i for i, s in enumerate(plan.segments) if s.kind == "clip")
        starts_at = _segment_offsets([s.seconds for s in plan.segments], plan.crossfade)[clip_index]
        assert starts_at > 1.0  # it is not the first segment
        assert _loudness(rendered.path, 0.0, starts_at - 0.5) < _loudness(
            rendered.path, starts_at + 0.2, starts_at + 1.0
        )


class TestMusicDucking:
    @pytest.fixture
    def tone(self, tmp_path: Path) -> Path:
        target = tmp_path / "tone.m4a"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=300:duration=30", "-c:a", "aac", str(target)],
            check=True,
        )  # fmt: skip
        return target

    def _reel_with_clip_and_music(self, trip, tmp_path, tone, **overrides):
        clip = FIXTURES / "clip_speech.mov"
        # Deliberately *between* two stills, not last: the music's tail fade lives at the end, and
        # a duck measured inside the fade is measuring the fade. That mistake made an earlier
        # version of these tests report 7.7 dB of "ducking" with ducking switched off.
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_speech.mov",
            "kind": "video",
            "taken_utc": "2026-07-18T09:01:30+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": float(_probe(clip, "format=duration"))},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        settings = {
            "clip_audio": True,
            "seconds_per_still": 2.0,
            "music_fade_seconds": 0.1,
            **overrides,
        }
        config = _fast_config(**settings)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        rendered = render_reel(plan, config, tmp_path, music=tone)
        clip_index = next(i for i, s in enumerate(plan.segments) if s.kind == "clip")
        starts = _segment_offsets([s.seconds for s in plan.segments], plan.crossfade)[clip_index]
        return rendered, plan, starts

    def test_the_music_is_quieter_under_the_clip_than_before_it(self, trip, tmp_path, tone):
        """Measured in the tone's own frequency band, so clip audio cannot be mistaken for it."""
        rendered, _, starts = self._reel_with_clip_and_music(trip, tmp_path, tone)
        before = _loudness(rendered.path, 0.5, starts - 0.5, band=300)
        under = _loudness(rendered.path, starts + 0.5, starts + 1.5, band=300)
        assert before - under > 3.0

    def test_the_music_recovers_after_the_clip(self, trip, tmp_path, tone):
        rendered, plan, starts = self._reel_with_clip_and_music(trip, tmp_path, tone)
        clip = next(s for s in plan.segments if s.kind == "clip")
        under = _loudness(rendered.path, starts + 0.5, starts + 1.5, band=300)
        after = _loudness(rendered.path, starts + clip.seconds + 1.0, plan.duration - 0.3, band=300)
        assert after > under

    def test_the_music_is_ducked_rather_than_muted(self, trip, tmp_path, tone):
        rendered, _, starts = self._reel_with_clip_and_music(trip, tmp_path, tone)
        assert _loudness(rendered.path, starts + 0.5, starts + 1.5, band=300) > -60.0

    def test_no_ducking_is_applied_without_clip_audio(self, trip, tmp_path, tone):
        rendered, _, starts = self._reel_with_clip_and_music(trip, tmp_path, tone, clip_audio=False)
        before = _loudness(rendered.path, 0.5, starts - 0.5, band=300)
        under = _loudness(rendered.path, starts + 0.5, starts + 1.5, band=300)
        assert abs(before - under) < 2.0

    def test_reel_json_records_that_the_music_was_ducked(self, trip, tmp_path, tone):
        self._reel_with_clip_and_music(trip, tmp_path, tone)
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert document["audio"]["music_ducked_under_clips"] is True
        assert document["audio"]["ducking"]["method"].startswith("sidechaincompress")

    def test_reel_json_does_not_claim_ducking_without_music(self, trip, tmp_path):
        clip = FIXTURES / "clip_speech.mov"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_speech.mov",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": 2.0},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config(clip_audio=True)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        render_reel(plan, config, tmp_path)
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert document["audio"]["music_ducked_under_clips"] is False
        assert document["audio"]["ducking"] is None


class TestReelJsonOnDisk:
    def test_is_written_beside_the_video(self, trip, tmp_path):
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        assert (tmp_path / "reel" / REEL_JSON_FILENAME).exists()

    def test_reports_the_real_rendered_size(self, trip, tmp_path):
        config = _fast_config()
        rendered = render_reel(build_plan(trip, config), config, tmp_path)
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert [document["video"]["width"], document["video"]["height"]] == [
            int(v) for v in _probe(rendered.path, "stream=width,height").split()
        ]

    def test_a_clips_timeline_position_matches_where_its_picture_starts(self, trip, tmp_path):
        """The number `docs/choosing_music.md` tells a reader to go and listen at."""
        clip = FIXTURES / "clip_speech.mov"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_speech.mov",
            "kind": "video",
            "taken_utc": "2026-07-18T09:01:30+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": float(_probe(clip, "format=duration"))},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config()
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        render_reel(plan, config, tmp_path)

        index = next(i for i, s in enumerate(plan.segments) if s.kind == "clip")
        expected = _segment_offsets([s.seconds for s in plan.segments], plan.crossfade)[index]
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        reported = document["excerpts"]["by_asset"]["vid"]["timeline_start_seconds"]
        assert reported == pytest.approx(expected, abs=0.3)

    def test_declares_music_absent_when_it_is(self, trip, tmp_path):
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert document["audio"]["music_supplied"] is False


class TestSubtitles:
    """A selectable track, in real Chinese bytes, muxed into a real MP4."""

    STORY = {
        "title": "Fixture Trip",
        "subtitle": "July 2026",
        "language": "en",
        "translations": {"zh": {"title": "固定行程", "subtitle": "2026年7月"}},
        "days": [
            {
                "date": "2026-07-18",
                "title": "A Day in Vienna",
                "narrative": "x",
                "translations": {"zh": {"title": "维也纳的一天"}},
            }
        ],
        "captions": [
            {"asset_id": "asset0", "caption": "A sharp one.", "translations": {"zh": "清晰的一张"}},
            {"asset_id": "asset1", "caption": "Another.", "translations": {"zh": "另一张"}},
            {"asset_id": "asset2", "caption": "No translation here."},
        ],
    }

    def _render(self, trip, tmp_path, languages, story=None):
        config = _fast_config()
        plan = build_plan(trip, config, story=story or self.STORY)
        rendered = render_reel(
            plan, config, tmp_path, story=story or self.STORY, subtitle_languages=languages
        )
        return rendered, plan

    def test_writes_a_vtt_beside_the_video(self, trip, tmp_path):
        self._render(trip, tmp_path, ["zh"])
        assert (tmp_path / "reel" / "trip.zh.vtt").exists()

    def test_the_vtt_holds_real_chinese_not_stripped_text(self, trip, tmp_path):
        """`renderable()` deletes CJK; a soft track must never go through it."""
        self._render(trip, tmp_path, ["zh"])
        body = (tmp_path / "reel" / "trip.zh.vtt").read_text(encoding="utf-8")
        assert "维也纳的一天" in body
        assert "清晰的一张" in body

    def test_the_vtt_is_valid_webvtt(self, trip, tmp_path):
        self._render(trip, tmp_path, ["zh"])
        body = (tmp_path / "reel" / "trip.zh.vtt").read_text(encoding="utf-8")
        assert body.startswith("WEBVTT")
        assert "-->" in body

    def test_the_track_is_muxed_into_the_video(self, trip, tmp_path):
        rendered, _ = self._render(trip, tmp_path, ["zh"])
        assert "subtitle" in _probe(rendered.path, "stream=codec_type")

    def test_the_muxed_track_carries_the_right_language_tag(self, trip, tmp_path):
        rendered, _ = self._render(trip, tmp_path, ["zh"])
        tags = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
             "stream_tags=language", "-of", "default=nw=1:nk=1", str(rendered.path)],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        assert "zho" in tags.stdout

    def test_the_video_still_plays_after_muxing(self, trip, tmp_path):
        rendered, plan = self._render(trip, tmp_path, ["zh"])
        assert _is_mp4(rendered.path)
        assert float(_probe(rendered.path, "format=duration")) == pytest.approx(
            plan.duration, abs=0.5
        )

    def test_two_languages_give_two_selectable_tracks(self, trip, tmp_path):
        rendered, _ = self._render(trip, tmp_path, ["zh", "en"])
        assert _probe(rendered.path, "stream=codec_type").count("subtitle") == 2

    def test_a_language_with_no_translations_gets_no_track(self, trip, tmp_path):
        """A Chinese track full of English would be the artifact overstating itself."""
        rendered, plan = self._render(trip, tmp_path, ["ja"])
        assert not (tmp_path / "reel" / "trip.ja.vtt").exists()
        assert "subtitle" not in _probe(rendered.path, "stream=codec_type")
        assert any("no 'ja' translations" in n for n in plan.notes)

    def test_a_partly_translated_language_is_written_and_reported(self, trip, tmp_path):
        _, plan = self._render(trip, tmp_path, ["zh"])
        assert (tmp_path / "reel" / "trip.zh.vtt").exists()
        assert any("no translation" in n for n in plan.notes)

    def test_reel_json_reports_the_track_and_its_coverage(self, trip, tmp_path):
        self._render(trip, tmp_path, ["zh"])
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        track = document["subtitles"]["tracks"][0]
        assert track["language"] == "zh"
        assert track["translated_cues"] < track["cues"]
        assert track["fully_translated"] is False

    def test_re_rendering_replaces_the_track_rather_than_appending(self, trip, tmp_path):
        """`-map 0` copied the input's existing subtitles, so a second render of the same reel
        appended a duplicate — and the stale one carries no language tag, so a player shows a
        nameless extra entry."""
        self._render(trip, tmp_path, ["zh"])
        rendered, _ = self._render(trip, tmp_path, ["zh"])
        assert _probe(rendered.path, "stream=codec_type").count("subtitle") == 1

    def test_re_rendering_with_a_different_language_does_not_keep_the_old_one(self, trip, tmp_path):
        self._render(trip, tmp_path, ["zh", "en"])
        rendered, _ = self._render(trip, tmp_path, ["en"])
        assert _probe(rendered.path, "stream=codec_type").count("subtitle") == 1

    def test_no_subtitle_request_means_no_track(self, trip, tmp_path):
        rendered, _ = self._render(trip, tmp_path, [])
        assert "subtitle" not in _probe(rendered.path, "stream=codec_type")

    def test_cues_do_not_overlap_in_the_rendered_file(self, trip, tmp_path):
        self._render(trip, tmp_path, ["zh"])
        body = (tmp_path / "reel" / "trip.zh.vtt").read_text(encoding="utf-8")
        stamps = re.findall(r"(\d\d:\d\d:\d\d\.\d\d\d) --> (\d\d:\d\d:\d\d\.\d\d\d)", body)

        def to_secs(s):
            h, m, rest = s.split(":")
            return int(h) * 3600 + int(m) * 60 + float(rest)

        spans = [(to_secs(a), to_secs(b)) for a, b in stamps]
        assert spans
        for (_, end), (start, _) in pairwise(spans):
            assert end <= start


def _band_bytes(path: Path, at: float, *, bottom: bool) -> bytes:
    """A downscaled greyscale strip from the top or bottom fifth of the frame at time `at`."""
    # crop is w:h:x:y -- all four. Dropping x silently centres y instead, which sampled the middle
    # of the frame for both bands and made them compare equal.
    crop = "in_w:in_h/5:0:in_h*4/5" if bottom else "in_w:in_h/5:0:0"
    dump = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
         "-vf", f"crop={crop},scale=64:16,format=gray", "-frames:v", "1",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    return dump.stdout


def _band_difference(a: Path, b: Path, at: float, *, bottom: bool) -> float:
    x, y = _band_bytes(a, at, bottom=bottom), _band_bytes(b, at, bottom=bottom)
    if not x or len(x) != len(y):
        return -1.0
    return sum(abs(p - q) for p, q in zip(x, y, strict=True)) / len(x)


class TestBurnIn:
    """Burn-in has to be checked in the pixels: nothing else distinguishes drawn text from a
    filter that silently drew an empty string, which is exactly what a CJK font gap causes."""

    def _render(self, trip, tmp_path, *, language="zh"):
        config = _fast_config()
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        rendered = render_reel(
            plan,
            config,
            tmp_path,
            story=TestSubtitles.STORY,
            subtitle_languages=[language],
            burn_in_language=language,
        )
        return rendered, plan

    def test_writes_a_separate_file_and_leaves_the_clean_reel_alone(self, trip, tmp_path):
        rendered, plan = self._render(trip, tmp_path)
        assert plan.burned_in == "trip.zh.mp4"
        assert (tmp_path / "reel" / "trip.zh.mp4").exists()
        assert rendered.path.name == "trip.mp4"

    def test_the_burned_copy_is_a_real_playable_mp4(self, trip, tmp_path):
        self._render(trip, tmp_path)
        burned = tmp_path / "reel" / "trip.zh.mp4"
        assert _is_mp4(burned)
        assert _probe(burned, "stream=codec_name").splitlines()[0] == "h264"

    def test_the_burned_copy_keeps_the_same_duration(self, trip, tmp_path):
        _, plan = self._render(trip, tmp_path)
        burned = tmp_path / "reel" / "trip.zh.mp4"
        assert float(_probe(burned, "format=duration")) == pytest.approx(plan.duration, abs=0.5)

    def test_text_actually_reaches_the_bottom_of_the_frame(self, trip, tmp_path):
        """The load-bearing assertion: pixels differ where the subtitle is drawn."""
        rendered, plan = self._render(trip, tmp_path)
        burned = tmp_path / "reel" / "trip.zh.mp4"
        cue = plan.subtitle_tracks[0].cues[0]
        at = (cue.start + cue.end) / 2
        assert _band_difference(rendered.path, burned, at, bottom=True) > 1.0

    def test_the_rest_of_the_picture_is_left_alone(self, trip, tmp_path):
        """A control: if the whole frame differed, the difference above would prove nothing."""
        rendered, plan = self._render(trip, tmp_path)
        burned = tmp_path / "reel" / "trip.zh.mp4"
        cue = plan.subtitle_tracks[0].cues[0]
        at = (cue.start + cue.end) / 2
        top = _band_difference(rendered.path, burned, at, bottom=False)
        bottom = _band_difference(rendered.path, burned, at, bottom=True)
        assert bottom > top * 2

    def test_audio_is_copied_not_dropped(self, trip, tmp_path):
        clip = FIXTURES / "clip_speech.mov"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_speech.mov",
            "kind": "video",
            "taken_utc": "2026-07-18T09:01:30+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": float(_probe(clip, "format=duration"))},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config()
        plan = build_plan(
            trip,
            config,
            story=TestSubtitles.STORY,
            clip_sources={"vid": ClipSource("original", clip)},
        )
        render_reel(
            plan,
            config,
            tmp_path,
            story=TestSubtitles.STORY,
            subtitle_languages=["zh"],
            burn_in_language="zh",
        )
        assert "audio" in _probe(tmp_path / "reel" / "trip.zh.mp4", "stream=codec_type")

    def test_reel_json_names_the_burned_file(self, trip, tmp_path):
        self._render(trip, tmp_path)
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert document["subtitles"]["burned_in_file"] == "trip.zh.mp4"

    def test_burning_a_language_with_no_track_is_declined_with_a_reason(self, trip, tmp_path):
        config = _fast_config()
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        render_reel(
            plan,
            config,
            tmp_path,
            story=TestSubtitles.STORY,
            subtitle_languages=["zh"],
            burn_in_language="ja",
        )
        assert plan.burned_in is None
        assert not (tmp_path / "reel" / "trip.ja.mp4").exists()
        assert any("cannot burn in 'ja'" in n for n in plan.notes)

    def test_no_font_for_the_text_declines_rather_than_drawing_blanks(self, trip, tmp_path, mocker):
        """Drawing an empty string would produce a file that looks finished and says nothing."""
        mocker.patch("story_book.export.reel.can_render", return_value=False)
        config = _fast_config()
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        render_reel(
            plan,
            config,
            tmp_path,
            story=TestSubtitles.STORY,
            subtitle_languages=["zh"],
            burn_in_language="zh",
        )
        assert plan.burned_in is None
        assert any("no font on this machine" in n for n in plan.notes)

    def test_a_scale_out_of_range_fails_before_any_encoding(self, trip, tmp_path):
        """Validated up front: a typo should not cost a full render first."""
        config = _fast_config(subtitle_scale=99.0)
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        with pytest.raises(ReelError, match="subtitle_scale"):
            render_reel(
                plan,
                config,
                tmp_path,
                story=TestSubtitles.STORY,
                subtitle_languages=["zh"],
                burn_in_language="zh",
            )
        assert not (tmp_path / "reel" / "trip.mp4").exists()

    def test_reel_json_records_the_font_size_actually_used(self, trip, tmp_path):
        config = _fast_config(subtitle_scale=1.5)
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        render_reel(
            plan,
            config,
            tmp_path,
            story=TestSubtitles.STORY,
            subtitle_languages=["zh"],
            burn_in_language="zh",
        )
        document = json.loads((tmp_path / "reel" / REEL_JSON_FILENAME).read_text())
        assert document["subtitles"]["burned_in_scale"] == 1.5
        assert document["subtitles"]["burned_in_font_px"] == cue_font_size(plan.height, 1.5)

    def test_a_bigger_scale_changes_more_of_the_frame(self, trip, tmp_path):
        """End to end: the scale reaches the rendered video, not just the cue PNGs."""
        differences = {}
        for scale in (1.0, 2.0):
            config = _fast_config(subtitle_scale=scale)
            plan = build_plan(trip, config, story=TestSubtitles.STORY)
            rendered = render_reel(
                plan,
                config,
                tmp_path,
                story=TestSubtitles.STORY,
                subtitle_languages=["zh"],
                burn_in_language="zh",
            )
            cue = plan.subtitle_tracks[0].cues[0]
            at = (cue.start + cue.end) / 2
            # The clean reel is cached and identical between runs; only the burned copy changes.
            differences[scale] = _band_difference(
                rendered.path, tmp_path / "reel" / "trip.zh.mp4", at, bottom=True
            )
        assert differences[2.0] > differences[1.0]

    def test_no_burn_in_request_writes_no_extra_file(self, trip, tmp_path):
        config = _fast_config()
        plan = build_plan(trip, config, story=TestSubtitles.STORY)
        render_reel(plan, config, tmp_path, story=TestSubtitles.STORY, subtitle_languages=["zh"])
        assert plan.burned_in is None
        assert not (tmp_path / "reel" / "trip.zh.mp4").exists()


class TestCueImages:
    def test_one_png_per_cue(self, tmp_path):
        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳", True), Cue(2, 4, "慕尼黑", True)])
        made = render_cue_images(track, 640, 360, tmp_path / "cues")
        assert len(made) == 2

    def test_images_are_the_frame_size_with_transparency(self, tmp_path):
        from PIL import Image

        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳", True)])
        _, path = render_cue_images(track, 640, 360, tmp_path / "cues")[0]
        with Image.open(path) as image:
            assert image.size == (640, 360)
            assert image.mode == "RGBA"

    def test_the_text_lands_in_the_lower_half(self, tmp_path):
        from PIL import Image

        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳的艺术与音乐", True)])
        _, path = render_cue_images(track, 640, 360, tmp_path / "cues")[0]
        with Image.open(path) as image:
            alpha = image.split()[-1]
            top = alpha.crop((0, 0, 640, 180)).getextrema()[1]
            bottom = alpha.crop((0, 180, 640, 360)).getextrema()[1]
        assert bottom > 0
        assert top == 0

    def test_a_bigger_scale_draws_more_text(self, tmp_path):
        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳的艺术与音乐", True)])
        small = render_cue_images(track, 640, 360, tmp_path / "s1", scale=1.0)[0][1]
        large = render_cue_images(track, 640, 360, tmp_path / "s2", scale=2.0)[0][1]
        assert _alpha_fraction(large) > _alpha_fraction(small) * 2

    def test_a_smaller_scale_draws_less(self, tmp_path):
        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳的艺术与音乐", True)])
        normal = render_cue_images(track, 640, 360, tmp_path / "s1", scale=1.0)[0][1]
        tiny = render_cue_images(track, 640, 360, tmp_path / "s3", scale=0.6)[0][1]
        assert _alpha_fraction(tiny) < _alpha_fraction(normal)

    def test_a_huge_scale_stays_inside_the_frame(self, tmp_path):
        """Text that covers more picture is the user's choice; text off-frame is a bug."""
        from PIL import Image

        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳的艺术与音乐，然后前往慕尼黑" * 3, True)])
        path = render_cue_images(track, 640, 360, tmp_path / "big", scale=4.0)[0][1]
        with Image.open(path) as image:
            assert image.size == (640, 360)
        assert _alpha_fraction(path) > 0

    def test_the_bottom_margin_is_configurable(self, tmp_path):
        from PIL import Image

        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        track = SubtitleTrack("zh", [Cue(0, 2, "维也纳", True)])
        low = render_cue_images(track, 640, 360, tmp_path / "low", bottom_margin=0.02)[0][1]
        high = render_cue_images(track, 640, 360, tmp_path / "high", bottom_margin=0.30)[0][1]

        def lowest_drawn_row(png):
            with Image.open(png) as image:
                alpha = image.split()[-1]
            return max(
                y for y in range(0, 360, 2) if alpha.crop((0, y, 640, y + 2)).getextrema()[1] > 0
            )

        assert lowest_drawn_row(low) > lowest_drawn_row(high)

    def test_long_text_without_spaces_still_wraps(self, tmp_path):
        """CJK has no word spaces, so space-only wrapping would overflow the frame."""
        from story_book.export.subtitles import Cue, SubtitleTrack, render_cue_images

        long_cjk = "维也纳的艺术与音乐" * 6
        track = SubtitleTrack("zh", [Cue(0, 2, long_cjk, True)])
        _, path = render_cue_images(track, 640, 360, tmp_path / "cues")[0]
        from PIL import Image

        with Image.open(path) as image:
            alpha = image.split()[-1]
            # Wrapped text occupies several rows; a single overflowing line would not.
            rows = [
                y for y in range(0, 360, 4) if alpha.crop((0, y, 640, y + 4)).getextrema()[1] > 0
            ]
        assert len(rows) > 4


class TestClipSourceResolution:
    def test_uses_a_package_proxy_when_the_source_tree_is_not_given(self, trip, tmp_path):
        trip["assets"]["vid"] = {"asset_id": "vid", "kind": "video", "filename": "c.mov"}
        proxy = tmp_path / "package" / "2026-07-18" / "video_proxies" / "vid.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"")
        assert resolve_clip_sources(trip, tmp_path)["vid"].role == "proxy"

    def test_the_original_is_preferred_over_a_proxy(self, trip, tmp_path):
        """A proxy is built small enough to upload to a chat -- 720p at CRF 28. Rendering from one
        and enlarging to 1080p threw away 59% of the detail on the real trip."""
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "kind": "video",
            "filename": "clip_silent.mp4",
        }
        proxy = tmp_path / "package" / "2026-07-18" / "video_proxies" / "vid.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"")

        chosen = resolve_clip_sources(trip, tmp_path, FIXTURES)["vid"]
        assert chosen.role == "original"
        assert chosen.path.parent == FIXTURES

    def test_the_resolved_source_carries_its_height(self, trip, tmp_path):
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "kind": "video",
            "filename": "clip_silent.mp4",
        }
        assert resolve_clip_sources(trip, tmp_path, FIXTURES)["vid"].height is not None

    def test_falls_back_to_the_source_tree(self, trip, tmp_path):
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "kind": "video",
            "filename": "clip_silent.mp4",
        }
        assert resolve_clip_sources(trip, tmp_path, FIXTURES)["vid"].role == "original"

    def test_a_clip_smaller_than_the_frame_is_reported(self, trip, tmp_path):
        """Silent about it, the reel looks soft next to the photographs for no stated reason."""
        clip = FIXTURES / "clip_silent.mp4"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_silent.mp4",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": 3.0},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config(height=2160)
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("proxy", clip, height=240)})
        assert plan.upscaled_clips == ["clip_silent.mp4"]
        assert any("enlarged to fit the frame" in note for note in plan.notes)

    def test_a_clip_at_or_above_the_frame_height_is_not_reported(self, trip, tmp_path):
        clip = FIXTURES / "clip_silent.mp4"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_silent.mp4",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": 3.0},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config()
        plan = build_plan(
            trip, config, clip_sources={"vid": ClipSource("original", clip, height=1080)}
        )
        assert plan.upscaled_clips == []

    def test_an_unfindable_clip_is_absent_rather_than_guessed(self, trip, tmp_path):
        trip["assets"]["vid"] = {"asset_id": "vid", "kind": "video", "filename": "nope.mov"}
        assert "vid" not in resolve_clip_sources(trip, tmp_path, FIXTURES)

    def test_an_ambiguous_filename_is_declined(self, trip, tmp_path, mocker):
        trip["assets"]["vid"] = {"asset_id": "vid", "kind": "video", "filename": "c.mov"}
        mocker.patch.object(Path, "rglob", return_value=[Path("/a/c.mov"), Path("/b/c.mov")])
        assert "vid" not in resolve_clip_sources(trip, tmp_path, FIXTURES)


class TestNonDestructive:
    def test_rendering_does_not_touch_the_source_tree(self, trip, tmp_path):
        """The reel reads originals under --source; constraint #1 still applies."""
        before = {p: p.stat().st_mtime_ns for p in sorted(FIXTURES.iterdir()) if p.is_file()}
        clip = FIXTURES / "clip_silent.mp4"
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "filename": "clip_silent.mp4",
            "kind": "video",
            "taken_utc": "2026-07-18T09:10:00+00:00",
            "day": "2026-07-18",
            "preview": "previews/asset0.jpg",
            "thumbnail": "previews/asset0.jpg",
            "video": {"duration_seconds": 2.0},
            "location": {"place": {"city": "Vienna"}},
        }
        trip["days"][0]["events"][0]["assets"].append("vid")
        config = _fast_config()
        plan = build_plan(trip, config, clip_sources={"vid": ClipSource("original", clip)})
        render_reel(plan, config, tmp_path)

        after = {p: p.stat().st_mtime_ns for p in sorted(FIXTURES.iterdir()) if p.is_file()}
        assert after == before

    def test_everything_written_stays_under_the_reel_directory(self, trip, tmp_path):
        config = _fast_config()
        before = {p for p in tmp_path.rglob("*")}
        render_reel(build_plan(trip, config), config, tmp_path)
        new = {p for p in tmp_path.rglob("*")} - before
        assert all((tmp_path / "reel") in p.parents or p == tmp_path / "reel" for p in new)


class TestTitleCard:
    def test_writes_a_png_at_the_frame_size(self, tmp_path):
        from story_book.export.reel import Segment

        target = tmp_path / "card.png"
        render_title_card(Segment(kind="title", seconds=2.5, title="Vienna"), 640, 360, target)
        assert _magic(target)[:8] == b"\x89PNG\r\n\x1a\n"

    def test_a_long_title_wraps_instead_of_overflowing(self, tmp_path):
        from PIL import Image

        from story_book.export.reel import Segment

        target = tmp_path / "card.png"
        segment = Segment(
            kind="title",
            seconds=2.5,
            title="A Very Long Chapter Title That Cannot Fit On One Line At All",
        )
        render_title_card(segment, 640, 360, target)
        with Image.open(target) as image:
            assert image.size == (640, 360)

    def test_accented_text_renders_without_boxes(self, tmp_path):
        from story_book.export.reel import Segment

        target = tmp_path / "card.png"
        render_title_card(
            Segment(kind="title", seconds=2.5, title="München", subtitle="July 17–20"),
            640,
            360,
            target,
        )
        assert _drawn_pixel_fraction(target) > 0.001

    def test_a_chinese_title_card_is_not_blank(self, tmp_path):
        """`renderable()` alone deletes CJK outright, so this card came out empty. `font_for`
        picks a font that can draw it -- and only the pixels can tell the difference."""
        from story_book.export.fonts import can_render
        from story_book.export.reel import Segment

        if not can_render("维也纳的艺术与音乐"):
            pytest.skip("no CJK font installed on this machine")

        chinese = tmp_path / "zh.png"
        blank = tmp_path / "blank.png"
        render_title_card(
            Segment(kind="title", seconds=2.5, title="维也纳的艺术与音乐"), 640, 360, chinese
        )
        render_title_card(Segment(kind="title", seconds=2.5, title=""), 640, 360, blank)
        assert _drawn_pixel_fraction(chinese) > _drawn_pixel_fraction(blank) + 0.001


class TestSingleDay:
    def test_renders_one_day_without_the_trip_card(self, trip, tmp_path):
        config = _fast_config()
        plan = build_plan(trip, config, only_day="2026-07-18")
        render_reel(plan, config, tmp_path)
        assert [s.kind for s in plan.segments].count("title") == 1

    def test_a_day_render_gets_its_own_filename(self, trip, tmp_path):
        """Otherwise rendering five days in a row leaves only the fifth."""
        config = _fast_config()
        rendered = render_reel(build_plan(trip, config, only_day="2026-07-18"), config, tmp_path)
        assert rendered.path.name == "trip.2026-07-18.mp4"

    def test_a_day_render_does_not_overwrite_the_whole_trip_reel(self, trip, tmp_path):
        config = _fast_config()
        whole = render_reel(build_plan(trip, config), config, tmp_path)
        before = whole.path.read_bytes()
        render_reel(build_plan(trip, config, only_day="2026-07-18"), config, tmp_path)
        assert whole.path.read_bytes() == before
        assert (tmp_path / "reel" / "trip.2026-07-18.mp4").exists()

    def test_the_day_manifest_is_separate_too(self, trip, tmp_path):
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        render_reel(build_plan(trip, config, only_day="2026-07-18"), config, tmp_path)
        assert (tmp_path / "reel" / REEL_JSON_FILENAME).exists()
        assert (tmp_path / "reel" / "reel.2026-07-18.json").exists()

    def test_the_manifest_names_the_file_it_describes(self, trip, tmp_path):
        config = _fast_config()
        render_reel(build_plan(trip, config, only_day="2026-07-18"), config, tmp_path)
        document = json.loads((tmp_path / "reel" / "reel.2026-07-18.json").read_text())
        assert document["video"]["file"] == "trip.2026-07-18.mp4"

    def test_day_segments_reuse_the_whole_trip_cache(self, trip, tmp_path):
        """The cache key is the segment spec, so a day render costs only its own title card."""
        config = _fast_config()
        render_reel(build_plan(trip, config), config, tmp_path)
        again = render_reel(build_plan(trip, config, only_day="2026-07-18"), config, tmp_path)
        assert again.segments_rendered == 0
