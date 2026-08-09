"""Splitting the upload, not the work.

`plan_parts` is the whole of the multi-day behaviour and the backend fixture is a single-day
trip, so the grouping is proved here against synthetic files of known size.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from story_book.export.package import plan_parts


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A package tree: three days of 100 bytes each, plus small shared files at the root."""
    (tmp_path / "README.md").write_bytes(b"x" * 10)
    (tmp_path / "prompt.md").write_bytes(b"x" * 10)
    (tmp_path / "manifest.json").write_bytes(b"x" * 10)
    (tmp_path / "schema").mkdir()
    (tmp_path / "schema" / "story.schema.json").write_bytes(b"x" * 10)
    for date in ("2026-07-01", "2026-07-02", "2026-07-03"):
        day = tmp_path / date / "media"
        day.mkdir(parents=True)
        (day / "a.jpg").write_bytes(b"x" * 100)
    return tmp_path


def members(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def dates_in(parts: list[tuple[list[Path], list[str]]]) -> list[list[str]]:
    return [days for _, days in parts]


class TestPlanParts:
    def test_everything_fits_in_one_part(self, root: Path) -> None:
        assert len(plan_parts(root, members(root), 10_000)) == 1

    def test_no_limit_means_one_part(self, root: Path) -> None:
        assert len(plan_parts(root, members(root), None)) == 1

    def test_it_splits_when_over_the_limit(self, root: Path) -> None:
        assert len(plan_parts(root, members(root), 150)) > 1

    def test_each_day_appears_in_exactly_one_part(self, root: Path) -> None:
        grouped = dates_in(plan_parts(root, members(root), 150))
        flat = [date for days in grouped for date in days]
        assert sorted(flat) == ["2026-07-01", "2026-07-02", "2026-07-03"]

    def test_no_file_is_dropped_or_duplicated(self, root: Path) -> None:
        """A split that loses a photograph is worse than no split."""
        packed = [path for files, _ in plan_parts(root, members(root), 150) for path in files]
        assert sorted(packed) == members(root)

    def test_a_day_is_never_divided_between_parts(self, root: Path) -> None:
        for files, days in plan_parts(root, members(root), 150):
            in_files = {
                p.relative_to(root).parts[0] for p in files if len(p.relative_to(root).parts) > 1
            }
            assert in_files - {"schema"} == set(days)

    def test_shared_files_travel_with_the_first_part(self, root: Path) -> None:
        """The prompt and manifest are what the reader opens first."""
        first, _ = plan_parts(root, members(root), 150)[0]
        assert root / "prompt.md" in first and root / "manifest.json" in first

    def test_a_day_larger_than_the_limit_gets_its_own_part(self, root: Path) -> None:
        grouped = dates_in(plan_parts(root, members(root), 50))
        assert all(len(days) == 1 for days in grouped)

    def test_days_stay_in_date_order(self, root: Path) -> None:
        flat = [date for days in dates_in(plan_parts(root, members(root), 150)) for date in days]
        assert flat == sorted(flat)
