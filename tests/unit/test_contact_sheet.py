from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from story_book.export.contact_sheet import (
    CellIndex,
    ContactSheetResult,
    SheetCell,
    _fit_crop,
    _fit_letterbox,
    _truncate_to_width,
    render_contact_sheets,
)


def landscape_image(w: int = 800, h: int = 600) -> Image.Image:
    return Image.new("RGB", (w, h), (200, 100, 50))


def portrait_image(w: int = 600, h: int = 900) -> Image.Image:
    return Image.new("RGB", (w, h), (50, 100, 200))


def patch_open_image(mocker, images_by_name: dict[str, Image.Image | None]):
    """Route `_open_image` to in-memory images keyed by path name -- no filesystem touched."""

    def fake_open(path: Path):
        return images_by_name.get(path.name)

    mocker.patch("story_book.export.contact_sheet._open_image", side_effect=fake_open)


class TestCellIndex:
    def test_label_is_zero_padded_sheet_dash_cell(self):
        assert CellIndex(sheet=3, cell=7).label == "03-07"

    def test_label_handles_double_digit_values(self):
        assert CellIndex(sheet=12, cell=20).label == "12-20"

    def test_str_matches_label(self):
        index = CellIndex(sheet=1, cell=1)
        assert str(index) == index.label


class TestRenderContactSheetsEmptyAndSingle:
    def test_zero_pairs_returns_empty_result_without_error(self, mocker):
        patch_open_image(mocker, {})
        result = render_contact_sheets([])
        assert result.sheets == ()
        assert result.skipped == ()

    def test_single_image_produces_one_sheet_one_cell(self, mocker):
        patch_open_image(mocker, {"a.jpg": landscape_image()})
        result = render_contact_sheets([(Path("a.jpg"), "A caption")])
        assert len(result.sheets) == 1
        assert len(result.sheets[0].cells) == 1
        assert result.sheets[0].cells[0].index.label == "01-01"


class TestRenderContactSheetsPagination:
    def test_more_pairs_than_cells_per_sheet_creates_a_second_sheet(self, mocker):
        pairs = [(Path(f"img{i}.jpg"), f"caption {i}") for i in range(18)]
        images = {f"img{i}.jpg": landscape_image() for i in range(18)}
        patch_open_image(mocker, images)

        result = render_contact_sheets(pairs, cells_per_sheet=16)

        assert len(result.sheets) == 2
        assert len(result.sheets[0].cells) == 16
        assert len(result.sheets[1].cells) == 2

    def test_sheet_and_cell_numbers_are_stable_and_sequential(self, mocker):
        pairs = [(Path(f"img{i}.jpg"), f"caption {i}") for i in range(20)]
        images = {f"img{i}.jpg": landscape_image() for i in range(20)}
        patch_open_image(mocker, images)

        result = render_contact_sheets(pairs, cells_per_sheet=16)

        labels = [cell.index.label for sheet in result.sheets for cell in sheet.cells]
        assert labels[:16] == [f"01-{n:02d}" for n in range(1, 17)]
        assert labels[16:] == [f"02-{n:02d}" for n in range(1, 5)]


class TestRenderContactSheetsIndexMatchesCaption:
    def test_every_cell_index_maps_to_its_own_caption(self, mocker):
        pairs = [(Path(f"img{i}.jpg"), f"unique caption {i}") for i in range(5)]
        images = {f"img{i}.jpg": landscape_image() for i in range(5)}
        patch_open_image(mocker, images)

        result = render_contact_sheets(pairs)

        for i, (path, caption) in enumerate(pairs):
            cell = result.sheets[0].cells[i]
            assert cell.index.label == f"01-{i + 1:02d}"
            assert cell.caption == caption
            assert cell.image_path == path

    def test_index_mapping_returns_label_to_caption_and_path(self, mocker):
        patch_open_image(mocker, {"a.jpg": landscape_image()})
        result = render_contact_sheets([(Path("a.jpg"), "Sunset over the bay")])

        mapping = result.index_mapping()

        assert mapping["01-01"] == ("Sunset over the bay", "a.jpg")


class TestRenderContactSheetsMixedOrientation:
    def test_landscape_and_portrait_pairs_both_render_without_error(self, mocker):
        patch_open_image(
            mocker,
            {"land.jpg": landscape_image(), "port.jpg": portrait_image()},
        )
        result = render_contact_sheets(
            [(Path("land.jpg"), "landscape"), (Path("port.jpg"), "portrait")]
        )
        assert len(result.sheets[0].cells) == 2
        assert result.sheets[0].image.size[0] > 0


class TestRenderContactSheetsUnreadableImages:
    def test_unreadable_image_is_skipped_and_reported_not_raised(self, mocker):
        patch_open_image(mocker, {"good.jpg": landscape_image(), "bad.jpg": None})

        result = render_contact_sheets([(Path("good.jpg"), "fine"), (Path("bad.jpg"), "corrupt")])

        assert len(result.sheets[0].cells) == 1
        assert result.sheets[0].cells[0].caption == "fine"
        assert result.skipped == ((Path("bad.jpg"), "unreadable image"),)

    def test_all_unreadable_produces_empty_sheets(self, mocker):
        patch_open_image(mocker, {"bad.jpg": None})
        result = render_contact_sheets([(Path("bad.jpg"), "corrupt")])
        assert result.sheets == ()
        assert len(result.skipped) == 1


class TestFitLetterbox:
    def test_preserves_aspect_ratio_without_cropping_landscape(self):
        image = landscape_image(800, 400)
        fitted = _fit_letterbox(image, 300, 300)
        assert fitted.size == (300, 300)
        # Full source frame is present: no pixel row/col of the original is cut off,
        # meaning the source, scaled down, must fit entirely within the box on both axes.
        scale = min(300 / 800, 300 / 400)
        assert round(800 * scale) <= 300
        assert round(400 * scale) <= 300

    def test_preserves_aspect_ratio_without_cropping_portrait(self):
        image = portrait_image(400, 800)
        fitted = _fit_letterbox(image, 300, 300)
        assert fitted.size == (300, 300)


class TestFitCrop:
    def test_fills_box_completely(self):
        image = landscape_image(800, 400)
        cropped = _fit_crop(image, 300, 300)
        assert cropped.size == (300, 300)


class TestTruncateToWidth:
    def test_short_text_is_unchanged(self):
        image = Image.new("RGB", (200, 50))
        draw = ImageDraw.Draw(image)
        from PIL import ImageFont

        font = ImageFont.load_default(size=12)
        result = _truncate_to_width(draw, "short", font, max_width=1000)
        assert result == "short"

    def test_long_text_is_truncated_with_ellipsis(self):
        image = Image.new("RGB", (200, 50))
        draw = ImageDraw.Draw(image)
        from PIL import ImageFont

        font = ImageFont.load_default(size=20)
        long_caption = "A very long caption that will not fit in a small cell width at all"
        result = _truncate_to_width(draw, long_caption, font, max_width=80)
        assert result.endswith("…")
        assert draw.textlength(result, font=font) <= 80

    def test_truncation_never_exceeds_max_width(self):
        image = Image.new("RGB", (200, 50))
        draw = ImageDraw.Draw(image)
        from PIL import ImageFont

        font = ImageFont.load_default(size=24)
        result = _truncate_to_width(draw, "x" * 200, font, max_width=50)
        assert draw.textlength(result, font=font) <= 50


class TestContactSheetResultStructure:
    def test_result_is_structured_not_just_paths(self):
        cell = SheetCell(index=CellIndex(1, 1), image_path=Path("a.jpg"), caption="c")
        result = ContactSheetResult(sheets=(), skipped=((Path("bad.jpg"), "unreadable"),))
        assert cell.index.label == "01-01"
        assert result.skipped[0][1] == "unreadable"
