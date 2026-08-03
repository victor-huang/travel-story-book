"""Module 14, part 1: render contact sheets -- labeled grid montages of thumbnails.

Why this exists at all (see Module 14 in the dev plan): ChatGPT cannot do vision on images
buried inside a zip, and the chat UI's attachment count is far below a day's photo total. A
contact sheet turns a day's highlights into a small number of JPEGs the model can actually
look at, each cell carrying a stable index (`03-07` = sheet 3, cell 7) that the accompanying
brief refers back to -- "the fountain in 03-07" has to resolve to the same photo for a human
and for the model. That index is therefore the load-bearing part of this module; the caption
truncation and letterboxing exist only in service of keeping the index legible.

Pure Pillow. No DB, no config, no pipeline dependency -- this module only knows about
`(image_path, caption)` pairs and returns structured results; it never decides what a caption
should say or which photos are "highlights" (that's Selection, Module 10, and the brief
builder, T41).

Design choices, made explicit because each was a real fork:

* **Letterbox, not crop.** A cell preserves the whole photo (resized to fit, padded to fill)
  rather than center-cropping it. Cropping risks cutting out the exact subject a caption or
  brief refers to ("the fountain in 03-07") -- for a montage whose entire purpose is being
  *looked at* and *referenced*, showing the full frame matters more than a tidy grid.
* **Caption truncates, index never does.** The `NN-NN` index prefix is always drawn in full;
  if the caption text does not fit the cell width it is the caption's tail that is elided
  with an ellipsis, never the index.
* **Font size scales with cell size**, using Pillow's bundled scalable default font
  (`ImageFont.load_default(size=...)`, available since Pillow 10.1) so legibility does not
  depend on a TTF being present on the machine.
* **Unreadable images are skipped, not fatal.** A corrupt or unopenable file is recorded in
  `ContactSheetResult.skipped` with a reason string; the sheet is built from whatever remains.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

from story_book.export.fonts import load_font, renderable

logger = logging.getLogger(__name__)

FitMode = Literal["letterbox", "crop"]

DEFAULT_CELLS_PER_SHEET = 16
DEFAULT_COLUMNS = 4
DEFAULT_TARGET_WIDTH = 1600
"""Sheet width in pixels. 1600px at ~4 columns keeps a JPEG comfortably under typical chat
upload size limits while staying legible at normal screen zoom."""

CELL_PADDING = 8
CAPTION_BAND_RATIO = 0.22
"""Fraction of a cell's height reserved for the caption band beneath the thumbnail."""

BACKGROUND_COLOR = (24, 24, 24)
CELL_BACKGROUND_COLOR = (40, 40, 40)
CAPTION_TEXT_COLOR = (255, 255, 255)
INDEX_TEXT_COLOR = (255, 210, 90)

JPEG_QUALITY = 85


@dataclass(frozen=True, slots=True)
class CellIndex:
    """A stable `sheet-cell` address, 1-based on both axes.

    Rendered as `NN-NN` (e.g. `03-07` = sheet 3, cell 7 of that sheet). This is the string a
    caller uses to build the brief's index mapping, and the string drawn on the cell itself --
    the two must always agree, which is exactly what the acceptance criterion checks.
    """

    sheet: int
    cell: int

    @property
    def label(self) -> str:
        return f"{self.sheet:02d}-{self.cell:02d}"

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
class SheetCell:
    """One rendered cell: its index, the source image it came from, and the caption drawn."""

    index: CellIndex
    image_path: Path
    caption: str


@dataclass(frozen=True, slots=True)
class ContactSheet:
    """One montage JPEG (1-based `sheet_number`) plus the cells rendered onto it."""

    sheet_number: int
    image: Image.Image
    cells: tuple[SheetCell, ...]


@dataclass(frozen=True, slots=True)
class ContactSheetResult:
    """Everything `render_contact_sheets` produced: the sheets, and anything it skipped."""

    sheets: tuple[ContactSheet, ...]
    skipped: tuple[tuple[Path, str], ...]

    def index_mapping(self) -> dict[str, tuple[str, str]]:
        """`{"03-07": (caption, image_path_str), ...}` -- what a brief builder wants."""
        return {
            cell.index.label: (cell.caption, str(cell.image_path))
            for sheet in self.sheets
            for cell in sheet.cells
        }


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A system font where one exists, else Pillow's bundled one -- see `fonts.py`.

    The bundled font is always present but has no `é ü ö à ñ – —`, so a place name like
    "München" drew as boxes. Cell labels now go through `renderable()` for the same reason.
    """
    return load_font(size)


def _open_image(path: Path) -> Image.Image | None:
    """Open and fully decode `path`, returning None (never raising) if it can't be read."""
    try:
        image = Image.open(path)
        image.load()
        return image.convert("RGB")
    except Exception as exc:  # noqa: BLE001 -- any decode failure means "skip", not "crash"
        logger.warning("contact sheet: skipping unreadable image %s (%s)", path, exc)
        return None


def _fit_letterbox(image: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize preserving aspect ratio, centered on a `box_w`x`box_h` background -- no cropping."""
    canvas = Image.new("RGB", (box_w, box_h), CELL_BACKGROUND_COLOR)
    src_w, src_h = image.size
    scale = min(box_w / src_w, box_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    canvas.paste(resized, ((box_w - new_w) // 2, (box_h - new_h) // 2))
    return canvas


def _fit_crop(image: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize to cover `box_w`x`box_h`, center-cropping the overflow. Not the default -- kept
    for callers who prefer a tidy grid over preserving the full frame."""
    src_w, src_h = image.size
    scale = max(box_w / src_w, box_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    return resized.crop((left, top, left + box_w, top + box_h))


def _truncate_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    """Shorten `text` with a trailing ellipsis until it fits `max_width`, if it doesn't already.

    Every label passes through here, which makes it the one place to guarantee the font can
    actually draw what it is handed.
    """
    text = renderable(text, font)
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    truncated = text
    while truncated and draw.textlength(truncated + ellipsis, font=font) > max_width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis


def _render_cell(
    cell_w: int,
    cell_h: int,
    image: Image.Image,
    index: CellIndex,
    caption: str,
    fit_mode: FitMode,
) -> Image.Image:
    """One complete cell: thumbnail on top, index + caption band beneath."""
    caption_h = max(1, round(cell_h * CAPTION_BAND_RATIO))
    thumb_h = cell_h - caption_h

    cell = Image.new("RGB", (cell_w, cell_h), CELL_BACKGROUND_COLOR)
    fitter = _fit_letterbox if fit_mode == "letterbox" else _fit_crop
    thumb = fitter(image, cell_w, thumb_h)
    cell.paste(thumb, (0, 0))

    draw = ImageDraw.Draw(cell)
    index_font_size = max(10, cell_h // 14)
    caption_font_size = max(9, cell_h // 16)
    index_font = _load_font(index_font_size, bold=True)
    caption_font = _load_font(caption_font_size)

    pad = max(2, cell_w // 60)
    text_x = pad
    text_y = thumb_h + pad

    index_label = index.label
    draw.text((text_x, text_y), index_label, font=index_font, fill=INDEX_TEXT_COLOR)
    index_width = draw.textlength(index_label + "  ", font=index_font)

    caption_x = text_x + int(index_width)
    caption_max_width = max(1, cell_w - caption_x - pad)
    truncated_caption = _truncate_to_width(draw, caption, caption_font, caption_max_width)
    caption_y = text_y + (index_font_size - caption_font_size) // 2
    draw.text((caption_x, caption_y), truncated_caption, font=caption_font, fill=CAPTION_TEXT_COLOR)

    return cell


def render_contact_sheets(
    pairs: list[tuple[Path, str]],
    *,
    cells_per_sheet: int = DEFAULT_CELLS_PER_SHEET,
    columns: int = DEFAULT_COLUMNS,
    target_width: int = DEFAULT_TARGET_WIDTH,
    fit_mode: FitMode = "letterbox",
) -> ContactSheetResult:
    """Render `pairs` of (image_path, caption) into one or more labeled contact sheet JPEGs.

    Paginates automatically: sheet 1 gets the first `cells_per_sheet` *readable* pairs, sheet
    2 the next, and so on. An unreadable image is skipped and reported in
    `ContactSheetResult.skipped` -- it consumes neither a cell nor a spot in the pagination
    count, so sheets stay full rather than gappy.

    Zero pairs (or zero readable pairs) returns an empty result, not an error.
    """
    if cells_per_sheet < 1:
        raise ValueError("cells_per_sheet must be at least 1")
    if columns < 1:
        raise ValueError("columns must be at least 1")

    skipped: list[tuple[Path, str]] = []
    readable: list[tuple[Path, str, Image.Image]] = []
    for path, caption in pairs:
        image = _open_image(path)
        if image is None:
            skipped.append((path, "unreadable image"))
            continue
        readable.append((path, caption, image))

    sheets: list[ContactSheet] = []
    rows = -(-cells_per_sheet // columns)  # ceil div
    cell_w = target_width // columns
    cell_h = round(cell_w * 1.15)  # slightly taller than wide to leave room for the caption band

    for sheet_number, start in enumerate(range(0, len(readable), cells_per_sheet), start=1):
        chunk = readable[start : start + cells_per_sheet]
        sheet_w = cell_w * columns
        sheet_h = cell_h * rows
        sheet_image = Image.new("RGB", (sheet_w, sheet_h), BACKGROUND_COLOR)

        cells: list[SheetCell] = []
        for offset, (path, caption, image) in enumerate(chunk):
            cell_number = offset + 1
            index = CellIndex(sheet=sheet_number, cell=cell_number)
            rendered = _render_cell(cell_w, cell_h, image, index, caption, fit_mode)
            row, col = divmod(offset, columns)
            sheet_image.paste(rendered, (col * cell_w, row * cell_h))
            cells.append(SheetCell(index=index, image_path=path, caption=caption))

        sheets.append(
            ContactSheet(sheet_number=sheet_number, image=sheet_image, cells=tuple(cells))
        )

    return ContactSheetResult(sheets=tuple(sheets), skipped=tuple(skipped))


def save_contact_sheets(
    result: ContactSheetResult, out_dir: Path, *, prefix: str = "contact_sheet"
) -> list[Path]:
    """Write each sheet as `<out_dir>/<prefix>_NN.jpg`. Returns the written paths, in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sheet in result.sheets:
        path = out_dir / f"{prefix}_{sheet.sheet_number:02d}.jpg"
        sheet.image.save(path, format="JPEG", quality=JPEG_QUALITY)
        written.append(path)
    return written
