from __future__ import annotations

from pathlib import Path

from PIL import Image

from story_book.export.contact_sheet import (
    CAPTION_BAND_RATIO,
    render_contact_sheets,
    save_contact_sheets,
)

# 20 real fixture stills, deliberately excluding the HEIC (needs pillow-heif registered to
# open, which is not this module's concern) and the video/notes files. Mixed orientation:
# receipt.jpg is portrait, the rest are landscape.
TWENTY_IMAGE_NAMES = [
    "blurred.jpg",
    "burst_a.jpg",
    "burst_b.jpg",
    "distinct_a.jpg",
    "distinct_b.jpg",
    "exact_a.jpg",
    "exact_b.jpg",
    "jpeg_gps_no_offset.jpg",
    "jpeg_no_exif.jpg",
    "jpeg_no_gps.jpg",
    "offset_gps_conflict.jpg",
    "overexposed.jpg",
    "receipt.jpg",
    "screenshot.jpg",
    "sharp.jpg",
    "tz_after_1.jpg",
    "tz_after_2.jpg",
    "tz_after_3.jpg",
    "tz_before_1.jpg",
    "underexposed.jpg",
]


def build_pairs(media_dir: Path) -> list[tuple[Path, str]]:
    captions = [
        "11:45 Hohensalzburg Fortress overlooking the old town",
        "Lunch at a small cafe near the river",
        "Burst shot A of the street performer",
        "Burst shot B of the street performer, same moment",
        "A distinct photo of the market square",
        "Another distinct photo taken moments later",
        "Exact duplicate frame A",
        "Exact duplicate frame B",
        "GPS present but no timezone offset recorded",
        "No EXIF data at all on this one",
        "No GPS coordinates available for this shot",
        "Offset conflicts with GPS-implied timezone",
        "Badly overexposed shot of the fountain",
        "Restaurant receipt kept for expense tracking",
        "Phone screenshot of a map, not a real photo",
        "A sharp, well-focused shot of the cathedral",
        "Timezone-after fixture, item 1",
        "Timezone-after fixture, item 2",
        "Timezone-after fixture, item 3",
        "Underexposed shot taken at dusk",
    ]
    return [
        (media_dir / name, caption)
        for name, caption in zip(TWENTY_IMAGE_NAMES, captions, strict=True)
    ]


class TestRenderContactSheetsWithRealFixtures:
    def test_twenty_image_sheet_indexes_match_captions(self, media_dir):
        pairs = build_pairs(media_dir)

        result = render_contact_sheets(pairs, cells_per_sheet=16, columns=4)

        assert result.skipped == ()
        all_cells = [cell for sheet in result.sheets for cell in sheet.cells]
        assert len(all_cells) == 20

        for cell, (path, caption) in zip(all_cells, pairs, strict=True):
            assert cell.caption == caption
            assert cell.image_path == path

        mapping = result.index_mapping()
        for cell in all_cells:
            assert mapping[cell.index.label][0] == cell.caption

    def test_renders_a_real_twenty_image_sheet_to_tmp_path(self, media_dir, tmp_path):
        """Render a real 20-image sheet to disk. Mechanically verifiable legibility signals
        are asserted below (font size relative to cell, caption band non-overlap with the
        thumbnail area, index label always present in full). Whether a human finds the result
        legible at a glance is NOT asserted here -- that judgement is still outstanding and
        needs an actual set of human eyes on the JPEG this test writes out."""
        pairs = build_pairs(media_dir)

        result = render_contact_sheets(pairs, cells_per_sheet=16, columns=4)
        written = save_contact_sheets(result, tmp_path, prefix="day01")

        assert len(written) == 2
        for path in written:
            assert path.exists()
            image = Image.open(path)
            image.verify()

    def test_caption_band_height_leaves_room_for_legible_text(self, media_dir, tmp_path):
        pairs = build_pairs(media_dir)[:1]
        result = render_contact_sheets(pairs, cells_per_sheet=16, columns=4)
        sheet_image = result.sheets[0].image
        cell_w = sheet_image.size[0] // 4
        cell_h = round(cell_w * 1.15)
        caption_band_h = round(cell_h * CAPTION_BAND_RATIO)

        # Font sizing in _render_cell derives from cell_h // 14 and cell_h // 16; the caption
        # band must be tall enough to hold that text without it spilling into the thumbnail
        # above or being clipped at the cell's bottom edge.
        index_font_size = max(10, cell_h // 14)
        caption_font_size = max(9, cell_h // 16)
        assert caption_band_h >= max(index_font_size, caption_font_size)

    def test_index_label_is_never_truncated_even_with_a_very_long_caption(self, media_dir):
        long_caption = "X" * 500
        pairs = [(media_dir / "sharp.jpg", long_caption)]

        result = render_contact_sheets(pairs, cells_per_sheet=16, columns=4)

        cell = result.sheets[0].cells[0]
        assert cell.index.label == "01-01"
        # The stored caption is untruncated (truncation happens only at draw time); the
        # index/caption mapping downstream (T41's brief) still gets the full text.
        assert cell.caption == long_caption

    def test_unreadable_fixture_is_skipped_without_crashing(self, media_dir, tmp_path):
        fake_image = tmp_path / "not_really_an_image.jpg"
        fake_image.write_bytes(b"not a jpeg at all")
        pairs = [(media_dir / "sharp.jpg", "a real photo"), (fake_image, "broken file")]

        result = render_contact_sheets(pairs)

        assert len(result.sheets[0].cells) == 1
        assert result.sheets[0].cells[0].caption == "a real photo"
        assert result.skipped == ((fake_image, "unreadable image"),)

    def test_mixed_portrait_and_landscape_fixtures_render_together(self, media_dir):
        pairs = [
            (media_dir / "receipt.jpg", "portrait receipt"),
            (media_dir / "sharp.jpg", "landscape cathedral"),
        ]
        result = render_contact_sheets(pairs)
        assert len(result.sheets[0].cells) == 2
