"""Unit tests for font resolution and transliteration.

The bug these exist for: Pillow's bundled font has no `é ü ö à ñ – —`, so a title card reading
"München" drew boxes. A box looks like a defect; "Munchen" looks like a limitation.
"""

from __future__ import annotations

import pytest
from PIL import ImageFont

from story_book.export.fonts import (
    FONT_CANDIDATES,
    can_render,
    coverage,
    font_for,
    font_identity,
    load_font,
    renderable,
    supports,
)


class TestLoadFont:
    def test_prefers_the_first_system_font_that_exists(self, mocker, tmp_path):
        real = tmp_path / "Real.ttf"
        real.write_bytes(b"")
        truetype = mocker.patch("story_book.export.fonts.ImageFont.truetype")
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", (str(real),))
        load_font.cache_clear()
        load_font(20)
        truetype.assert_called_once_with(str(real), size=20)

    def test_falls_back_to_the_bundled_font_when_nothing_is_installed(self, mocker):
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", ("/nope/Missing.ttf",))
        default = mocker.patch("story_book.export.fonts.ImageFont.load_default")
        load_font.cache_clear()
        load_font(20)
        default.assert_called_once_with(size=20)

    def test_skips_a_candidate_that_exists_but_will_not_load(self, mocker, tmp_path):
        broken = tmp_path / "Broken.ttf"
        broken.write_bytes(b"not a font")
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", (str(broken),))
        default = mocker.patch("story_book.export.fonts.ImageFont.load_default")
        load_font.cache_clear()
        load_font(20)
        default.assert_called_once_with(size=20)

    def test_candidates_cover_both_platforms_ci_runs_on(self):
        joined = " ".join(FONT_CANDIDATES)
        assert "/System/Library/Fonts" in joined
        assert "/usr/share/fonts" in joined


class TestFontIdentity:
    def test_reports_the_resolved_path(self, mocker, tmp_path):
        real = tmp_path / "Real.ttf"
        real.write_bytes(b"")
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", (str(real),))
        assert font_identity() == str(real)

    def test_reports_the_fallback_by_name(self, mocker):
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", ("/nope/Missing.ttf",))
        assert font_identity() == "pillow-default"


class TestRenderableWithTheBundledFont:
    """The bundled font is the constrained case, so it is the one worth asserting against."""

    def setup_method(self):
        self.font = ImageFont.load_default(size=24)

    def test_an_en_dash_becomes_a_hyphen(self):
        assert renderable("July 17–20", self.font) == "July 17-20"

    def test_an_em_dash_becomes_a_hyphen(self):
        assert renderable("Vienna — Munich", self.font) == "Vienna - Munich"

    def test_accents_are_stripped_rather_than_dropped(self):
        assert renderable("München", self.font) == "Munchen"

    def test_a_word_with_several_accents_stays_a_word(self):
        assert renderable("Café Pestsäule", self.font) == "Cafe Pestsaule"

    def test_plain_ascii_is_returned_untouched(self):
        assert renderable("Vienna in Art and Music", self.font) == "Vienna in Art and Music"

    def test_a_non_breaking_space_becomes_a_space(self):
        assert renderable("17 20", self.font) == "17 20"

    def test_nothing_renders_as_a_replacement_box(self):
        text = renderable("naïve — Café, 50°C", self.font)
        assert all(supports(self.font, char) for char in text)


class TestRenderableWithAFullFont:
    def test_a_font_with_coverage_keeps_the_original_typography(self):
        load_font.cache_clear()  # earlier tests patch FONT_CANDIDATES and prime the cache
        font = load_font(24)
        if not supports(font, "ü"):
            return  # no system font here; the bundled-font tests above cover that path
        assert renderable("München — Café", font) == "München — Café"


class TestCoverage:
    def test_full_coverage_for_ascii_in_a_latin_font(self):
        assert coverage(ImageFont.load_default(size=24), "Vienna") == 1.0

    def test_zero_coverage_for_cjk_in_the_bundled_font(self):
        assert coverage(ImageFont.load_default(size=24), "维也纳") == 0.0

    def test_whitespace_only_text_counts_as_covered(self):
        assert coverage(ImageFont.load_default(size=24), "   ") == 1.0

    def test_partial_coverage_is_reported_as_a_fraction(self):
        assert coverage(ImageFont.load_default(size=24), "ab维") == pytest.approx(2 / 3)


class TestFontFor:
    """The bug: `load_font` returns Arial for everything, and `renderable` then deletes CJK
    entirely -- an empty string, not a row of boxes."""

    def test_latin_text_keeps_a_latin_font(self):
        font = font_for("Cathedrals and Palaces", 40)
        assert coverage(font, "Cathedrals and Palaces") == 1.0

    def test_chinese_text_gets_a_font_that_can_draw_it(self):
        if not can_render("维也纳的艺术"):
            pytest.skip("no CJK font installed on this machine")
        assert coverage(font_for("维也纳的艺术", 40), "维也纳的艺术") == 1.0

    def test_chinese_text_survives_renderable_with_the_chosen_font(self):
        if not can_render("维也纳的艺术"):
            pytest.skip("no CJK font installed on this machine")
        text = "维也纳的艺术"
        assert renderable(text, font_for(text, 40)) == text

    def test_falls_back_to_the_best_available_when_nothing_covers_it(self, mocker):
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", ())
        mocker.patch("story_book.export.fonts.CJK_FONT_CANDIDATES", ())
        assert font_for("维也纳", 40) is not None

    def test_never_returns_none(self):
        assert font_for("", 40) is not None


class TestCanRender:
    def test_true_for_plain_ascii(self):
        assert can_render("Vienna in Art and Music")

    def test_false_for_something_no_font_here_has(self):
        """Used to refuse burn-in honestly rather than drawing blanks."""
        assert not can_render("🎻🎺🥁")

    def test_reports_false_when_no_fonts_are_available(self, mocker):
        mocker.patch("story_book.export.fonts.FONT_CANDIDATES", ())
        mocker.patch("story_book.export.fonts.CJK_FONT_CANDIDATES", ())
        assert not can_render("维也纳")


class TestSupports:
    def test_whitespace_always_counts_as_supported(self):
        assert supports(ImageFont.load_default(size=24), " ")

    def test_a_missing_glyph_is_reported_missing(self):
        assert not supports(ImageFont.load_default(size=24), "ü")

    def test_a_present_glyph_is_reported_present(self):
        assert supports(ImageFont.load_default(size=24), "V")
