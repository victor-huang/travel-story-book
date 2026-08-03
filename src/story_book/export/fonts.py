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
