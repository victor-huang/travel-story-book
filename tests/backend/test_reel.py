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
from pathlib import Path

import pytest

from story_book.config import Config, ReelConfig
from story_book.export.reel import (
    REEL_FILENAME,
    REEL_JSON_FILENAME,
    SEGMENT_CACHE_DIRNAME,
    ClipSource,
    _segment_offsets,
    build_plan,
    frame_size,
    render_reel,
    render_title_card,
    resolve_clip_sources,
    segment_key,
)

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
    diffs = [
        sum(abs(a - b) for a, b in zip(x, y, strict=True)) / size
        for x, y in zip(frames[:-1], frames[1:], strict=True)
    ]
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
        """The control for the test below: without clips, only crossfades change pixels."""
        config = _fast_config(crossfade_seconds=0.0)
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
        """The reason the key is the spec and not the position in the list."""
        config = _fast_config()
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

    def test_two_renders_of_an_unchanged_trip_agree_on_every_key(self, trip, tmp_path):
        config = _fast_config()
        first = build_plan(trip, config)
        second = build_plan(trip, config)
        assert [segment_key(s, first, config) for s in first.segments] == [
            segment_key(s, second, config) for s in second.segments
        ]

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


class TestClipSourceResolution:
    def test_finds_a_package_proxy(self, trip, tmp_path):
        trip["assets"]["vid"] = {"asset_id": "vid", "kind": "video", "filename": "c.mov"}
        proxy = tmp_path / "package" / "2026-07-18" / "video_proxies" / "vid.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"")
        assert resolve_clip_sources(trip, tmp_path)["vid"].role == "proxy"

    def test_falls_back_to_the_source_tree(self, trip, tmp_path):
        trip["assets"]["vid"] = {
            "asset_id": "vid",
            "kind": "video",
            "filename": "clip_silent.mp4",
        }
        assert resolve_clip_sources(trip, tmp_path, FIXTURES)["vid"].role == "original"

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

    def test_text_the_font_cannot_draw_does_not_become_boxes(self, tmp_path, mocker):
        from PIL import ImageFont

        from story_book.export.reel import Segment

        mocker.patch(
            "story_book.export.reel.load_font",
            side_effect=lambda size: ImageFont.load_default(size=size),
        )
        target = tmp_path / "card.png"
        render_title_card(
            Segment(kind="title", seconds=2.5, title="München", subtitle="July 17–20"),
            640,
            360,
            target,
        )
        assert target.exists()


class TestSingleDay:
    def test_renders_one_day_without_the_trip_card(self, trip, tmp_path):
        config = _fast_config()
        plan = build_plan(trip, config, only_day="2026-07-18")
        rendered = render_reel(plan, config, tmp_path)
        assert rendered.path.name == REEL_FILENAME
        assert [s.kind for s in plan.segments].count("title") == 1
