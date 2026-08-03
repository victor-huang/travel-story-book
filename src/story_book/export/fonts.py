"""Text rendering for the image exports (contact sheets, reel title cards).

Pillow's bundled scalable font is always present, which is why `contact_sheet.py` reached for it
-- but it is **ASCII-only in practice**: `é ü ö à ñ – — £ €` all render as .notdef boxes. On a
trip through Vienna and Munich that is not hypothetical, and a tofu box in a title card is worse
than a plain-ASCII fallback because it looks like a bug rather than a limitation.

So: prefer a real system font with Latin coverage, and if none exists, transliterate whatever the
chosen font cannot draw. "München" becomes "Munchen", never "M□nchen".
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_CANDIDATES: tuple[str, ...] = (
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

CJK_FONT_CANDIDATES: tuple[str, ...] = (
    # macOS. `PingFang.ttc` exists but Pillow cannot open it, so it is deliberately absent.
    "/System/Library/Fonts/Supplemental/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
)
"""Tried only when the Latin fonts cannot draw the text in hand -- see `font_for`."""

# Typographic characters a model's prose reliably produces, mapped to ASCII that any font has.
# Applied only when the font actually lacks the character.
PUNCTUATION: dict[str, str] = {
    "–": "-",
    "—": "-",
    "‒": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",
    " ": " ",
    "×": "x",
    "€": "EUR",
    "£": "GBP",
}


@lru_cache(maxsize=32)
def load_font(size: int) -> ImageFont.FreeTypeFont:
    """A scalable font at `size`, preferring one with real Latin coverage."""
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=64)
def _try_load(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    if not Path(path).exists():
        return None
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return None


def coverage(font: ImageFont.FreeTypeFont, text: str) -> float:
    """Fraction of `text`'s non-space characters this font can actually draw."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    return sum(1 for c in chars if supports(font, c)) / len(chars)


def font_for(text: str, size: int) -> ImageFont.FreeTypeFont:
    """The first available font that can draw *every* character of `text`.

    Chosen by what has to be rendered rather than by a fixed default. This is the difference
    between a Chinese caption appearing and disappearing: `load_font` returns Arial, which has no
    CJK glyphs, and `renderable` then drops every one of them -- an empty string, not a row of
    boxes. Latin fonts are tried first so English text keeps the same look it always had.
    """
    best: tuple[float, ImageFont.FreeTypeFont] | None = None
    for path in (*FONT_CANDIDATES, *CJK_FONT_CANDIDATES):
        font = _try_load(path, size)
        if font is None:
            continue
        score = coverage(font, text)
        if score >= 1.0:
            return font
        if best is None or score > best[0]:
            best = (score, font)
    return best[1] if best is not None else ImageFont.load_default(size=size)


def can_render(text: str, size: int = 40) -> bool:
    """Whether any available font can draw all of `text`. Used to refuse burn-in honestly."""
    return coverage(font_for(text, size), text) >= 1.0


def font_identity() -> str:
    """Which font `load_font` resolves to here.

    Belongs in any cache key covering rendered text: installing a font changes the pixels, and a
    cache that cannot see that keeps serving the ASCII-transliterated version forever.
    """
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return "pillow-default"


@lru_cache(maxsize=8)
def _missing_glyph(font: ImageFont.FreeTypeFont) -> bytes:
    """The .notdef mask, which every unsupported character renders as."""
    return bytes(font.getmask("￾"))


def supports(font: ImageFont.FreeTypeFont, char: str) -> bool:
    if char in " \t\n":
        return True
    try:
        return bytes(font.getmask(char)) != _missing_glyph(font)
    except Exception:  # noqa: BLE001 -- an unrenderable char must degrade, never crash a render
        return False


def renderable(text: str, font: ImageFont.FreeTypeFont) -> str:
    """`text` with every character the font cannot draw replaced by one it can.

    Tries the punctuation table first, then Unicode decomposition (é -> e), and drops what
    survives both. A dropped character is strictly better than a box: the reader sees a word
    with a plain letter instead of a rendering failure.
    """
    if all(supports(font, char) for char in text):
        return text

    out: list[str] = []
    for char in text:
        if supports(font, char):
            out.append(char)
            continue
        replacement = PUNCTUATION.get(char)
        if replacement is None:
            stripped = unicodedata.normalize("NFKD", char)
            replacement = "".join(c for c in stripped if not unicodedata.combining(c))
        out.append("".join(c for c in replacement if supports(font, c)))
    return "".join(out)
