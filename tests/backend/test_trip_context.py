from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from story_book.trip_context import TripContext, TripContextError

EXAMPLE_CONTEXT = Path(__file__).parents[2] / "trip_context.example.toml"


class TestLoadFromFile:
    def test_missing_path_yields_empty_context(self, tmp_path: Path) -> None:
        context = TripContext.load(tmp_path / "absent.toml")
        assert context.is_empty is True

    def test_real_file_is_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "context.toml"
        path.write_text('journal_voice = "first_person_singular"\nnotes = ["it mattered"]\n')
        context = TripContext.load(path)
        assert context.journal_voice == "first_person_singular"
        assert context.notes == ("it mattered",)

    def test_file_with_only_roles_produces_usable_context(self, tmp_path: Path) -> None:
        path = tmp_path / "context.toml"
        path.write_text('[[travelers]]\nrole = "narrator"\n\n[[travelers]]\nrole = "spouse"\n')
        context = TripContext.load(path)
        assert [t.role for t in context.travelers] == ["narrator", "spouse"]
        assert all(t.name is None for t in context.travelers)
        assert context.is_empty is False

    def test_unknown_key_in_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "context.toml"
        path.write_text('typo_field = "oops"\n')
        with pytest.raises(TripContextError, match="typo_field"):
            TripContext.load(path)


class TestExampleContext:
    def test_example_context_is_valid(self) -> None:
        with EXAMPLE_CONTEXT.open("rb") as handle:
            raw = tomllib.load(handle)
        context = TripContext.from_dict(raw)
        assert context.is_empty is False

    def test_example_context_documents_optional_traveler_without_name(self) -> None:
        with EXAMPLE_CONTEXT.open("rb") as handle:
            raw = tomllib.load(handle)
        context = TripContext.from_dict(raw)
        assert any(t.name is None for t in context.travelers)
