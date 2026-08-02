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

    def test_an_unknown_top_level_section_in_a_file_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "context.toml"
        path.write_text('typo_field = "oops"\nnotes = ["kept"]\n')

        assert TripContext.load(path).notes == ("kept",)


class TestYamlIsAccepted:
    """A model asked to summarise a trip returns YAML unprompted, and the real one did."""

    def test_a_yaml_context_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "context.yaml"
        path.write_text(
            "journal_voice: first_person_plural\n"
            "notes:\n  - The concert was why we came.\n"
            "travelers:\n  - role: son\n    name: Aiden\n"
        )
        context = TripContext.load(path)

        assert context.journal_voice == "first_person_plural"
        assert context.travelers[0].name == "Aiden"
        assert context.notes == ("The concert was why we came.",)

    def test_a_yml_extension_also_works(self, tmp_path: Path) -> None:
        path = tmp_path / "context.yml"
        path.write_text("notes:\n  - It rained.\n")

        assert TripContext.load(path).notes == ("It rained.",)

    def test_malformed_yaml_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "context.yaml"
        path.write_text("notes:\n  - [unclosed\n")
        with pytest.raises(TripContextError, match="not valid YAML"):
            TripContext.load(path)

    def test_a_yaml_scalar_is_not_a_context(self, tmp_path: Path) -> None:
        path = tmp_path / "context.yaml"
        path.write_text("just a string\n")
        with pytest.raises(TripContextError, match="mapping"):
            TripContext.load(path)

    def test_an_empty_yaml_file_is_an_empty_context(self, tmp_path: Path) -> None:
        path = tmp_path / "context.yaml"
        path.write_text("")

        assert TripContext.load(path).is_empty


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
