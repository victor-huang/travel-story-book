"""Unit tests for the reel planner. No ffmpeg, no filesystem -- `build_plan` is pure by design."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from story_book.config import Config, ReelConfig
from story_book.export.reel import (
    REEL_VERSION,
    ClipSource,
    ReelError,
    ReelPlan,
    ReelSelection,
    Segment,
    StoryScene,
    _audio_graph,
    _clip_excerpt,
    _fill_filter,
    _segment_offsets,
    _xfade_chain,
    build_plan,
    frame_size,
    parse_aspect,
    reel_filenames,
    segment_key,
    write_reel_json,
)


def _asset(asset_id: str, **overrides) -> dict:
    asset = {
        "asset_id": asset_id,
        "filename": f"{asset_id}.jpeg",
        "kind": "image",
        "taken_utc": "2026-07-18T09:00:00+00:00",
        "day": "2026-07-18",
        "preview": f"previews/{asset_id}.jpg",
        "thumbnail": f"thumbs/{asset_id}.jpg",
        "location": {"place": {"city": "Vienna"}},
    }
    asset.update(overrides)
    return asset


def _video(asset_id: str, duration: float, **overrides) -> dict:
    return _asset(
        asset_id,
        kind="video",
        filename=f"{asset_id}.mov",
        video={"duration_seconds": duration, "poster": f".cache/video/{asset_id}_poster.jpg"},
        **overrides,
    )


def _doc(assets: list[dict], *, highlights: list[str] | None = None) -> dict:
    by_id = {a["asset_id"]: a for a in assets}
    return {
        "trip": {"name": "Europe 2026", "start_local": "2026-07-18", "end_local": "2026-07-20"},
        "assets": by_id,
        "days": [
            {
                "date": "2026-07-18",
                "highlights": highlights if highlights is not None else list(by_id),
                "events": [{"id": "2026-07-18#1", "assets": list(by_id)}],
            }
        ],
    }


class TestParseAspect:
    def test_parses_width_and_height(self):
        assert parse_aspect("16:9") == (16, 9)

    def test_vertical_ratio_is_just_as_valid(self):
        assert parse_aspect("9:16") == (9, 16)

    def test_rejects_a_single_number(self):
        with pytest.raises(ReelError, match="16:9"):
            parse_aspect("1.78")

    def test_rejects_zero(self):
        with pytest.raises(ReelError, match="positive"):
            parse_aspect("16:0")


class TestFrameSize:
    def test_sixteen_by_nine_at_1080_is_full_hd(self):
        assert frame_size(Config()) == (1920, 1080)

    def test_vertical_swaps_the_axes(self):
        config = Config(reel=ReelConfig(aspect="9:16"))
        assert frame_size(config) == (608, 1080)

    def test_dimensions_are_always_even_for_yuv420p(self):
        config = Config(reel=ReelConfig(aspect="7:5", height=1001))
        width, height = frame_size(config)
        assert width % 2 == 0 and height % 2 == 0


class TestBuildPlanOrdering:
    def test_orders_by_utc_not_by_asset_id(self):
        early = _asset("zzz", taken_utc="2026-07-18T08:00:00+00:00")
        late = _asset("aaa", taken_utc="2026-07-18T20:00:00+00:00")
        plan = build_plan(_doc([late, early]), Config())
        stills = [s.asset_id for s in plan.segments if s.kind == "still"]
        assert stills == ["zzz", "aaa"]

    def test_opens_with_a_trip_title_card(self):
        plan = build_plan(_doc([_asset("a")]), Config())
        assert plan.segments[0].kind == "title"
        assert plan.segments[0].title == "Europe 2026"

    def test_each_day_gets_its_own_title_card(self):
        plan = build_plan(_doc([_asset("a")]), Config())
        titles = [s for s in plan.segments if s.kind == "title"]
        assert len(titles) == 2  # trip, then the one day

    def test_a_single_day_render_has_no_trip_card(self):
        plan = build_plan(_doc([_asset("a")]), Config(), only_day="2026-07-18")
        assert [s.kind for s in plan.segments].count("title") == 1

    def test_unknown_day_is_an_error_not_an_empty_reel(self):
        with pytest.raises(ReelError, match="no day"):
            build_plan(_doc([_asset("a")]), Config(), only_day="1999-01-01")

    def test_refuses_to_render_a_reel_with_nothing_in_it(self):
        with pytest.raises(ReelError, match="nothing to render"):
            build_plan(_doc([], highlights=[]), Config())


class TestBuildPlanSelection:
    def test_uses_day_highlights_not_every_photo(self):
        assets = [_asset("a"), _asset("b"), _asset("c")]
        plan = build_plan(_doc(assets, highlights=["a"]), Config())
        assert [s.asset_id for s in plan.segments if s.kind == "still"] == ["a"]

    def test_videos_appear_even_when_not_highlighted(self):
        assets = [_asset("a"), _video("v", 40.0)]
        plan = build_plan(_doc(assets, highlights=["a"]), Config())
        assert "v" in [s.asset_id for s in plan.segments]

    def test_day_title_prefers_the_story_over_the_place(self):
        story = {"days": [{"date": "2026-07-18", "title": "A Day of Museums"}]}
        plan = build_plan(_doc([_asset("a")]), Config(), story=story)
        assert plan.segments[1].title == "A Day of Museums"

    def test_day_title_falls_back_to_the_place_without_a_story(self):
        plan = build_plan(_doc([_asset("a")]), Config())
        assert plan.segments[1].title == "Vienna"

    def test_a_still_with_no_derived_image_is_skipped(self):
        asset = _asset("a")
        asset["preview"] = asset["thumbnail"] = None
        plan = build_plan(_doc([asset, _asset("b")]), Config())
        assert [s.asset_id for s in plan.segments if s.kind == "still"] == ["b"]


def _multiday_doc() -> dict:
    """Three days in two regions, so date and place filters can be told apart."""
    assets = {}
    for index, (asset_id, date, city, region) in enumerate(
        [
            ("t1", "2026-07-05", "Mayrhofen", "Tyrol"),
            ("t2", "2026-07-05", "Schmirn", "Tyrol"),
            ("s1", "2026-07-14", "Salzburg", "Salzburg"),
            ("s2", "2026-07-14", "Hallein", "Salzburg"),
            ("v1", "2026-07-18", "Vienna", "Vienna"),
        ]
    ):
        assets[asset_id] = _asset(
            asset_id,
            day=date,
            taken_utc=f"{date}T0{index}:00:00+00:00",
            location={"place": {"city": city, "region": region, "country": "AT"}},
        )
    days = {}
    for asset_id, asset in assets.items():
        days.setdefault(asset["day"], []).append(asset_id)
    return {
        "trip": {"name": "Europe 2026", "start_local": "2026-06-28", "end_local": "2026-07-23"},
        "assets": assets,
        "days": [
            {
                "date": date,
                "highlights": ids,
                "events": [{"id": f"{date}#1", "assets": ids}],
            }
            for date, ids in sorted(days.items())
        ],
    }


class TestReelSelection:
    """A 22-day trip is 13 minutes as one montage, so it wants cutting into parts -- and the seams
    are geographic. Dates cannot express "the Salzburg leg" when a travel day straddles two
    regions; places cannot separate two visits to one city. Both compose."""

    def _stills(self, plan) -> list[str]:
        return [s.asset_id for s in plan.segments if s.kind == "still"]

    def test_a_date_floor_drops_earlier_days(self):
        plan = build_plan(
            _multiday_doc(), Config(), selection=ReelSelection(date_from="2026-07-14")
        )
        assert self._stills(plan) == ["s1", "s2", "v1"]

    def test_a_date_ceiling_drops_later_days_inclusively(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(date_to="2026-07-14"))
        assert self._stills(plan) == ["t1", "t2", "s1", "s2"]

    def test_a_place_filter_selects_across_days(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("Salzburg",)))
        assert self._stills(plan) == ["s1", "s2"]

    def test_a_place_matches_region_as_well_as_city(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("Tyrol",)))
        assert self._stills(plan) == ["t1", "t2"]

    def test_place_matching_is_case_insensitive(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("vIeNnA",)))
        assert self._stills(plan) == ["v1"]

    def test_several_places_are_a_union(self):
        plan = build_plan(
            _multiday_doc(), Config(), selection=ReelSelection(places=("Vienna", "Hallein"))
        )
        assert self._stills(plan) == ["s2", "v1"]

    def test_dates_and_places_compose_with_and(self):
        """The case that needs both: a travel day where only one region belongs to this part."""
        plan = build_plan(
            _multiday_doc(),
            Config(),
            selection=ReelSelection(date_from="2026-07-14", places=("Salzburg",)),
        )
        assert self._stills(plan) == ["s1", "s2"]

    def test_a_day_left_empty_by_a_place_filter_gets_no_title_card(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("Vienna",)))
        assert [s.day for s in plan.segments if s.kind == "title"] == [None, "2026-07-18"]

    def test_a_selection_matching_nothing_is_an_error(self):
        with pytest.raises(ReelError, match="no highlights or previews match"):
            build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("Lisbon",)))

    def test_a_date_range_covering_no_day_is_an_error(self):
        with pytest.raises(ReelError, match="no day between"):
            build_plan(
                _multiday_doc(),
                Config(),
                selection=ReelSelection(date_from="2027-01-01", date_to="2027-02-01"),
            )

    def test_the_name_becomes_the_opening_title_card(self):
        plan = build_plan(
            _multiday_doc(), Config(), selection=ReelSelection(places=("Vienna",), name="Vienna")
        )
        assert plan.segments[0].title == "Vienna"

    def test_a_narrowed_reel_still_opens_with_a_title_card(self):
        plan = build_plan(_multiday_doc(), Config(), selection=ReelSelection(places=("Vienna",)))
        assert plan.segments[0].kind == "title"
        assert plan.segments[0].day is None


class TestReelSelectionSlugs:
    def test_the_whole_trip_has_no_slug_so_it_keeps_trip_mp4(self):
        assert ReelSelection().slug is None
        assert reel_filenames(None) == ("trip.mp4", "reel.json")

    def test_a_name_becomes_the_filename(self):
        assert reel_filenames(ReelSelection(name="Salzburg & the Lakes").slug)[0] == (
            "trip.salzburg-the-lakes.mp4"
        )

    def test_a_single_day_keeps_its_date_as_the_slug(self):
        assert ReelSelection(day="2026-07-18").slug == "2026-07-18"

    def test_places_form_a_slug_when_unnamed(self):
        assert ReelSelection(places=("Vienna",)).slug == "vienna"

    def test_a_date_range_forms_a_slug_when_unnamed(self):
        assert ReelSelection(date_from="2026-06-28", date_to="2026-07-11").slug == (
            "2026-06-28_2026-07-11"
        )

    def test_slugs_never_contain_path_separators(self):
        assert "/" not in ReelSelection(name="a/b c").slug

    def test_an_unusable_name_still_yields_something(self):
        assert ReelSelection(name="!!!").slug == "part"


class TestClipHandling:
    def test_a_clip_with_footage_becomes_a_clip_segment(self):
        doc = _doc([_video("v", 40.0)])
        sources = {"v": ClipSource("proxy", Path("/tmp/v.mp4"))}
        plan = build_plan(doc, Config(), clip_sources=sources)
        clip = next(s for s in plan.segments if s.kind == "clip")
        assert clip.seconds == pytest.approx(5.0)

    def test_a_clip_with_no_footage_becomes_its_poster(self):
        plan = build_plan(_doc([_video("v", 40.0)]), Config())
        segment = next(s for s in plan.segments if s.asset_id == "v")
        assert segment.kind == "still"
        assert segment.source_role == "poster"

    def test_a_clip_without_footage_is_reported_not_hidden(self):
        plan = build_plan(_doc([_video("v", 40.0)]), Config())
        assert plan.clips_as_stills == ["v.mov"]
        assert any("no proxy" in note for note in plan.notes)

    def test_a_story_supplied_range_wins_over_the_arbitrary_one(self):
        doc = _doc([_video("v", 100.0)])
        story = {
            "video_scenes": [
                {"asset_id": "v", "source_start_seconds": 40, "source_end_seconds": 47}
            ]
        }
        sources = {"v": ClipSource("proxy", Path("/tmp/v.mp4"))}
        plan = build_plan(doc, Config(), story=story, clip_sources=sources)
        clip = next(s for s in plan.segments if s.kind == "clip")
        assert (clip.clip_start, clip.excerpt) == (40.0, "story_range")


class TestClipExcerpt:
    def test_a_short_clip_is_used_whole(self):
        asset = _video("v", 3.0)
        assert _clip_excerpt(asset, Config(), None) == (0.0, 3.0, "whole_clip")

    def test_a_long_clip_starts_at_the_poster_offset_and_says_it_is_arbitrary(self):
        start, seconds, why = _clip_excerpt(_video("v", 112.0), Config(), None)
        assert (start, seconds, why) == (1.0, 5.0, "fixed_head")

    def test_a_clip_too_short_to_cut_to_is_declined(self):
        assert _clip_excerpt(_video("v", 0.4), Config(), None) is None

    def test_a_zero_duration_clip_is_declined(self):
        assert _clip_excerpt(_video("v", 0.0), Config(), None) is None

    def test_a_story_range_beyond_the_clip_is_clamped_not_trusted(self):
        result = _clip_excerpt(_video("v", 10.0), Config(), StoryScene(8.0, 30.0, None))
        assert result is not None
        start, seconds, _ = result
        assert start + seconds <= 10.0

    def test_the_storys_own_timeline_duration_wins_over_its_source_range(self):
        """A story that cuts 0-12s but asks for 6s on screen gets 6s. Honouring the range instead
        turned 67 clips into 12.4 minutes where the story had asked for 6.5."""
        result = _clip_excerpt(_video("v", 40.0), Config(), StoryScene(0.0, 12.0, 6.0))
        assert result == (0.0, 6.0, "story_range")

    def test_a_story_range_is_still_capped_by_clip_max_seconds(self):
        config = Config(reel=ReelConfig(clip_max_seconds=4.0))
        result = _clip_excerpt(_video("v", 40.0), config, StoryScene(0.0, 12.0, 10.0))
        assert result == (0.0, 4.0, "story_range")

    def test_an_arbitrary_excerpt_is_capped_too(self):
        config = Config(reel=ReelConfig(clip_seconds=30.0, clip_max_seconds=6.0))
        _, seconds, why = _clip_excerpt(_video("v", 120.0), config, None)
        assert (seconds, why) == (6.0, "fixed_head")


class TestSegmentKey:
    def test_the_same_spec_hashes_the_same_way_twice(self):
        plan = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        segment = Segment(kind="still", seconds=3.0, asset_id="a", source="previews/a.jpg")
        assert segment_key(segment, plan, Config()) == segment_key(segment, plan, Config())

    def test_a_different_duration_is_a_different_segment(self):
        plan = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        one = Segment(kind="still", seconds=3.0, asset_id="a")
        assert segment_key(one, plan, Config()) != segment_key(
            replace(one, seconds=4.0), plan, Config()
        )

    def test_a_different_frame_size_is_a_different_segment(self):
        segment = Segment(kind="still", seconds=3.0, asset_id="a")
        wide = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        tall = ReelPlan(segments=[], width=608, height=1080, fps=30, crossfade=0.6)
        assert segment_key(segment, wide, Config()) != segment_key(segment, tall, Config())

    def test_position_in_the_reel_is_not_part_of_the_key(self):
        """Inserting a photo at the front must not invalidate everything behind it."""
        plan = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        segment = Segment(kind="still", seconds=3.0, asset_id="a")
        first = build_plan(_doc([_asset("a"), _asset("b")]), Config())
        second = build_plan(
            _doc([_asset("a"), _asset("b"), _asset("aa", taken_utc="2026-07-18T00:01:00+00:00")]),
            Config(),
        )
        keys_first = {segment_key(s, plan, Config()) for s in first.segments if s.asset_id == "b"}
        keys_second = {segment_key(s, plan, Config()) for s in second.segments if s.asset_id == "b"}
        assert keys_first == keys_second
        assert segment_key(segment, plan, Config())

    def test_the_font_is_part_of_a_title_cards_key(self, mocker):
        """Installing a font changes the pixels; a cache blind to that serves stale cards."""
        plan = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        title = Segment(kind="title", seconds=2.5, title="Vienna")
        mocker.patch("story_book.export.reel.font_identity", return_value="/fonts/a.ttf")
        first = segment_key(title, plan, Config())
        mocker.patch("story_book.export.reel.font_identity", return_value="/fonts/b.ttf")
        assert segment_key(title, plan, Config()) != first


class TestSegmentOffsets:
    def test_the_first_segment_starts_at_zero(self):
        assert _segment_offsets([3.0, 3.0], 0.6)[0] == 0.0

    def test_each_offset_accounts_for_the_overlap(self):
        assert _segment_offsets([3.0, 3.0, 3.0], 0.6) == pytest.approx([0.0, 2.4, 4.8])

    def test_offsets_agree_with_the_xfade_chain(self):
        """Clip audio must land where its picture does; two accumulations that disagree drift."""
        durations = [2.5, 3.0, 5.0, 3.0]
        chain, _ = _xfade_chain(durations, 0.6)
        offsets = _segment_offsets(durations, 0.6)
        assert [f"offset={o:.3f}" in chain for o in offsets[1:]] == [True] * 3


class TestAudioGraph:
    def _plan(self, **kwargs):
        return ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6, **kwargs)

    def test_no_music_and_no_audible_clips_means_no_soundtrack(self):
        assert _audio_graph(self._plan(), Config(), [3.0], [], None, 3.0) is None

    def test_clip_audio_alone_still_produces_a_track(self):
        result = _audio_graph(self._plan(), Config(), [3.0, 5.0], [1], None, 7.4)
        assert result is not None
        assert result[1] == "[clipbus]"

    def test_each_audible_clip_is_delayed_to_its_own_offset(self):
        parts, _ = _audio_graph(self._plan(), Config(), [2.5, 3.0, 5.0], [2], None, 9.3)
        assert "adelay=4300:all=1" in ";".join(parts)

    def test_music_alone_needs_no_ducking(self):
        parts, label = _audio_graph(self._plan(), Config(), [3.0], [], 1, 3.0)
        assert label == "[music]"
        assert "sidechaincompress" not in ";".join(parts)

    def test_music_plus_clips_ducks_the_music(self):
        parts, label = _audio_graph(self._plan(), Config(), [3.0, 5.0], [1], 2, 7.4)
        assert "sidechaincompress" in ";".join(parts)
        assert label == "[aout]"

    def test_the_clip_bus_is_split_so_it_is_both_heard_and_used_as_the_key(self):
        """Referencing one filter output twice is an ffmpeg error, not a warning."""
        parts, _ = _audio_graph(self._plan(), Config(), [3.0, 5.0], [1], 2, 7.4)
        assert "asplit=2" in ";".join(parts)

    def test_a_silent_bed_covers_the_whole_reel(self):
        parts, _ = _audio_graph(self._plan(), Config(), [3.0, 5.0], [1], None, 7.4)
        assert "anullsrc" in ";".join(parts)

    def test_the_configured_duck_ratio_is_used(self):
        config = Config(reel=ReelConfig(music_duck_ratio=13.0))
        parts, _ = _audio_graph(self._plan(), config, [3.0, 5.0], [1], 2, 7.4)
        assert "ratio=13.0" in ";".join(parts)


class TestClipAudioPlanning:
    def test_clip_audio_is_requested_when_enabled(self):
        doc = _doc([_video("v", 40.0)])
        sources = {"v": ClipSource("proxy", Path("/tmp/v.mp4"))}
        plan = build_plan(doc, Config(), clip_sources=sources)
        assert next(s for s in plan.segments if s.kind == "clip").with_audio is True

    def test_clip_audio_can_be_turned_off(self):
        doc = _doc([_video("v", 40.0)])
        sources = {"v": ClipSource("proxy", Path("/tmp/v.mp4"))}
        config = Config(reel=ReelConfig(clip_audio=False))
        plan = build_plan(doc, config, clip_sources=sources)
        assert next(s for s in plan.segments if s.kind == "clip").with_audio is False

    def test_turning_clip_audio_on_changes_the_segment_key(self):
        """It changes the rendered file, so it has to change the cache entry."""
        plan = ReelPlan(segments=[], width=1920, height=1080, fps=30, crossfade=0.6)
        clip = Segment(kind="clip", seconds=5.0, asset_id="v", with_audio=False)
        assert segment_key(clip, plan, Config()) != segment_key(
            replace(clip, with_audio=True), plan, Config()
        )

    def test_stills_never_ask_for_audio(self):
        plan = build_plan(_doc([_asset("a")]), Config())
        assert all(not s.with_audio for s in plan.segments if s.kind != "clip")


class TestXfadeChain:
    def test_a_single_segment_needs_no_crossfade(self):
        chain, label = _xfade_chain([3.0], 0.6)
        assert "xfade" not in chain
        assert label == "[vout]"

    def test_the_first_offset_accounts_for_the_overlap(self):
        chain, _ = _xfade_chain([3.0, 3.0], 0.6)
        assert "offset=2.400" in chain

    def test_offsets_accumulate_across_segments(self):
        chain, _ = _xfade_chain([3.0, 3.0, 3.0], 0.6)
        assert "offset=2.400" in chain and "offset=4.800" in chain

    def test_every_segment_is_joined(self):
        chain, _ = _xfade_chain([2.0] * 5, 0.5)
        assert chain.count("xfade") == 4


class TestPlanDuration:
    def test_crossfades_shorten_the_total(self):
        plan = ReelPlan(
            segments=[Segment(kind="still", seconds=3.0) for _ in range(4)],
            width=1920,
            height=1080,
            fps=30,
            crossfade=0.5,
        )
        assert plan.duration == pytest.approx(12.0 - 1.5)


class TestFillFilter:
    def test_the_photo_is_scaled_to_fit_so_nothing_is_cropped_away(self):
        assert "force_original_aspect_ratio=decrease" in _fill_filter(1920, 1080)

    def test_the_background_covers_the_frame(self):
        assert "force_original_aspect_ratio=increase" in _fill_filter(1920, 1080)

    def test_the_background_is_blurred(self):
        assert "gblur" in _fill_filter(1920, 1080)

    def test_the_input_is_split_rather_than_reused(self):
        """Referencing one input label twice is an ffmpeg error, not a warning."""
        assert "split=2" in _fill_filter(1920, 1080)


class TestReelJson:
    def _write(self, tmp_path: Path, plan: ReelPlan, music: Path | None = None) -> dict:
        target = write_reel_json(plan, Config(), tmp_path, music=music, duration=161.0)
        return json.loads(target.read_text())

    def _plan(self, segments: list[Segment]) -> ReelPlan:
        return ReelPlan(segments=segments, width=1920, height=1080, fps=30, crossfade=0.6)

    def test_says_when_no_music_was_supplied(self, tmp_path):
        document = self._write(tmp_path, self._plan([Segment(kind="still", seconds=3.0)]))
        assert document["audio"]["music_supplied"] is False

    def test_never_claims_beat_alignment(self, tmp_path):
        document = self._write(tmp_path, self._plan([Segment(kind="still", seconds=3.0)]))
        assert document["audio"]["beat_aligned"] is False

    def test_records_the_music_filename_when_there_is_one(self, tmp_path):
        music = tmp_path / "track.m4a"
        document = self._write(tmp_path, self._plan([Segment(kind="still", seconds=3.0)]), music)
        assert document["audio"]["music_filename"] == "track.m4a"

    def test_records_why_each_excerpt_was_chosen(self, tmp_path):
        clip = Segment(
            kind="clip",
            seconds=5.0,
            asset_id="v",
            filename="v.mov",
            clip_start=1.0,
            excerpt="fixed_head",
        )
        document = self._write(tmp_path, self._plan([clip]))
        assert document["excerpts"]["by_asset"]["v"]["chosen_by"] == "fixed_head"

    def test_distinguishes_the_source_offset_from_the_timeline_position(self, tmp_path):
        """Two different questions: which footage was used, and when to go and listen to it."""
        clip = Segment(kind="clip", seconds=5.0, asset_id="v", filename="v.mov", clip_start=1.0)
        plan = self._plan([clip])
        plan.clip_timeline_starts = {"v": 42.1}
        record = self._write(tmp_path, plan)["excerpts"]["by_asset"]["v"]
        assert record["source_start_seconds"] == 1.0
        assert record["timeline_start_seconds"] == 42.1

    def test_names_the_clips_that_became_stills(self, tmp_path):
        plan = self._plan([Segment(kind="still", seconds=3.0)])
        plan.clips_as_stills = ["IMG_1792.mov"]
        document = self._write(tmp_path, plan)
        assert document["video_sources"]["clips_rendered_as_stills"] == ["IMG_1792.mov"]

    def test_carries_the_reel_version(self, tmp_path):
        document = self._write(tmp_path, self._plan([Segment(kind="still", seconds=3.0)]))
        assert document["reel_version"] == REEL_VERSION
