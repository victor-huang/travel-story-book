"""Unit tests for subtitle cue building and formatting. No ffmpeg, no filesystem."""

from __future__ import annotations

from itertools import pairwise

from story_book.export.reel import Segment
from story_book.export.subtitles import (
    ISO_639_2,
    Cue,
    SubtitleTrack,
    build_cues,
    source_language,
    to_srt,
    to_webvtt,
)

STORY = {
    "title": "Vienna in Art and Music",
    "subtitle": "July 17-20, 2026",
    "translations": {"zh": {"title": "维也纳的艺术与音乐", "subtitle": "2026年7月17日至20日"}},
    "days": [
        {
            "date": "2026-07-18",
            "title": "Cathedrals and Palaces",
            "translations": {"zh": {"title": "大教堂与宫殿"}},
        }
    ],
    "captions": [
        {
            "asset_id": "a",
            "caption": "A tram on the Ring.",
            "translations": {"zh": "环城大道上的电车"},
        },
        {"asset_id": "b", "caption": "Untranslated on purpose."},
    ],
}


def _segments():
    return [
        Segment(kind="title", seconds=2.5, title="Vienna in Art and Music", subtitle="July 17-20"),
        Segment(kind="title", seconds=2.5, title="Cathedrals and Palaces", day="2026-07-18"),
        Segment(kind="still", seconds=3.0, asset_id="a", filename="a.jpg"),
        Segment(kind="still", seconds=3.0, asset_id="b", filename="b.jpg"),
        Segment(kind="still", seconds=3.0, asset_id="nocaption", filename="c.jpg"),
    ]


def _offsets(crossfade=0.5):
    out, running = [], 0.0
    for seg in _segments():
        out.append(running)
        running += seg.seconds - crossfade
    return out


class TestSourceLanguage:
    def test_defaults_to_english(self):
        assert source_language({}) == "en"

    def test_reads_the_declared_language(self):
        assert source_language({"language": "DE"}) == "de"

    def test_handles_no_story(self):
        assert source_language(None) == "en"


class TestBuildCues:
    def test_translates_the_trip_title_card(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        assert "维也纳的艺术与音乐" in track.cues[0].text

    def test_includes_the_translated_subtitle_line(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        assert "2026年7月17日至20日" in track.cues[0].text

    def test_translates_a_day_title_card(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        assert track.cues[1].text == "大教堂与宫殿"

    def test_translates_a_photo_caption(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        assert track.cues[2].text == "环城大道上的电车"

    def test_an_untranslated_caption_falls_back_and_is_flagged(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        cue = track.cues[3]
        assert cue.text == "Untranslated on purpose."
        assert cue.translated is False

    def test_an_asset_with_no_caption_gets_no_cue(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh")
        assert all("c.jpg" not in c.text for c in track.cues)
        assert len(track.cues) == 4

    def test_the_native_language_uses_the_original_text(self):
        track = build_cues(_segments(), _offsets(), STORY, "en")
        assert track.cues[2].text == "A tram on the Ring."

    def test_the_native_language_counts_as_translated(self):
        """Otherwise an English track would report itself as 0% translated."""
        track = build_cues(_segments(), _offsets(), STORY, "en")
        assert track.fully_translated

    def test_a_language_with_no_translations_produces_no_translated_cues(self):
        track = build_cues(_segments(), _offsets(), STORY, "ja")
        assert track.translated_count == 0
        assert track.cues  # the cues exist, they are just all fallbacks

    def test_captions_can_be_excluded(self):
        track = build_cues(_segments(), _offsets(), STORY, "zh", include_captions=False)
        assert len(track.cues) == 2

    def test_cues_start_where_their_segment_does(self):
        offsets = _offsets()
        track = build_cues(_segments(), offsets, STORY, "zh")
        assert track.cues[1].start == offsets[1]

    def test_no_story_means_nothing_is_translated(self):
        """Title cards still have their own text, but none of it is a translation -- so
        `_write_subtitles` refuses to write the track rather than labelling English as Chinese."""
        track = build_cues(_segments(), _offsets(), None, "zh")
        assert track.translated_count == 0


class TestCueClamping:
    def test_cues_never_overlap(self):
        """Segments overlap by the crossfade; two overlapping VTT cues render stacked."""
        track = build_cues(_segments(), _offsets(crossfade=0.5), STORY, "zh")
        for earlier, later in pairwise(track.cues):
            assert earlier.end <= later.start

    def test_a_cue_is_shortened_rather_than_dropped(self):
        track = build_cues(_segments(), _offsets(crossfade=0.5), STORY, "zh")
        assert all(c.end > c.start for c in track.cues)

    def test_the_last_cue_keeps_its_full_length(self):
        segments, offsets = _segments(), _offsets(crossfade=0.5)
        track = build_cues(segments, offsets, STORY, "zh")
        assert track.cues[-1].end == offsets[3] + segments[3].seconds


class TestWebVtt:
    def test_starts_with_the_required_header(self):
        assert to_webvtt(SubtitleTrack("zh", [])).startswith("WEBVTT")

    def test_formats_a_timestamp_with_a_period(self):
        track = SubtitleTrack("zh", [Cue(1.5, 4.25, "hi", True)])
        assert "00:00:01.500 --> 00:00:04.250" in to_webvtt(track)

    def test_carries_the_cue_text(self):
        track = SubtitleTrack("zh", [Cue(0.0, 2.0, "维也纳", True)])
        assert "维也纳" in to_webvtt(track)

    def test_numbers_cues_from_one(self):
        track = SubtitleTrack("zh", [Cue(0.0, 1.0, "a", True), Cue(1.0, 2.0, "b", True)])
        body = to_webvtt(track)
        assert "\n1\n" in body and "\n2\n" in body

    def test_handles_an_hour_long_reel(self):
        track = SubtitleTrack("zh", [Cue(3725.0, 3726.0, "x", True)])
        assert "01:02:05.000" in to_webvtt(track)


class TestSrt:
    def test_formats_a_timestamp_with_a_comma(self):
        track = SubtitleTrack("zh", [Cue(1.5, 4.25, "hi", True)])
        assert "00:00:01,500 --> 00:00:04,250" in to_srt(track)

    def test_has_no_webvtt_header(self):
        assert not to_srt(SubtitleTrack("zh", [Cue(0.0, 1.0, "a", True)])).startswith("WEBVTT")


class TestTrackStats:
    def test_an_empty_track_is_not_fully_translated(self):
        assert SubtitleTrack("zh", []).fully_translated is False

    def test_a_mixed_track_is_not_fully_translated(self):
        track = SubtitleTrack("zh", [Cue(0, 1, "a", True), Cue(1, 2, "b", False)])
        assert track.fully_translated is False
        assert track.translated_count == 1


class TestLanguageCodes:
    def test_mandarin_maps_to_the_iso_639_2_code(self):
        assert ISO_639_2["zh"] == "zho"

    def test_english_maps_to_eng(self):
        assert ISO_639_2["en"] == "eng"
