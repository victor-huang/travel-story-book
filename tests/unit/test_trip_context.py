from __future__ import annotations

import pytest

from story_book.trip_context import Traveler, TripContext, TripContextError


class TestEmptyContext:
    def test_default_construction_is_empty(self) -> None:
        assert TripContext().is_empty is True

    def test_load_with_none_path_is_empty(self) -> None:
        assert TripContext.load(None).is_empty is True

    def test_from_dict_with_empty_mapping_is_empty(self) -> None:
        assert TripContext.from_dict({}).is_empty is True

    def test_context_with_any_field_set_is_not_empty(self) -> None:
        assert TripContext(notes=("mattered",)).is_empty is False


class TestFromDict:
    def test_only_roles_no_names_is_usable(self) -> None:
        context = TripContext.from_dict({"travelers": [{"role": "narrator"}, {"role": "child"}]})
        assert context.travelers == (Traveler(role="narrator"), Traveler(role="child"))
        assert context.is_empty is False

    def test_traveler_name_is_optional_alias(self) -> None:
        context = TripContext.from_dict({"travelers": [{"role": "narrator", "name": "V."}]})
        assert context.travelers[0].name == "V."

    def test_notes_are_preserved_verbatim(self) -> None:
        note = "The concert was one of the main reasons for coming."
        context = TripContext.from_dict({"notes": [note]})
        assert context.notes == (note,)

    def test_known_plans_are_preserved(self) -> None:
        context = TripContext.from_dict({"known_plans": ["Mozart concert, 20:15"]})
        assert context.known_plans == ("Mozart concert, 20:15",)

    def test_journal_voice_is_parsed(self) -> None:
        context = TripContext.from_dict({"journal_voice": "first_person_singular"})
        assert context.journal_voice == "first_person_singular"


class TestValidation:
    def test_unknown_top_level_key_names_it(self) -> None:
        with pytest.raises(TripContextError, match="unknown key"):
            TripContext.from_dict({"nonsense": True})

    def test_unknown_top_level_key_error_names_the_key(self) -> None:
        with pytest.raises(TripContextError, match="nonsense"):
            TripContext.from_dict({"nonsense": True})

    def test_invalid_journal_voice_is_rejected(self) -> None:
        with pytest.raises(TripContextError, match="journal_voice"):
            TripContext.from_dict({"journal_voice": "third_person"})

    def test_traveler_missing_role_is_rejected(self) -> None:
        with pytest.raises(TripContextError, match="role"):
            TripContext.from_dict({"travelers": [{"name": "V."}]})

    def test_traveler_unknown_key_names_it(self) -> None:
        with pytest.raises(TripContextError, match="nickname"):
            TripContext.from_dict({"travelers": [{"role": "narrator", "nickname": "V."}]})

    def test_travelers_must_be_a_list(self) -> None:
        with pytest.raises(TripContextError, match="travelers"):
            TripContext.from_dict({"travelers": {"role": "narrator"}})


class TestRendering:
    def test_rendered_block_contains_voice(self) -> None:
        context = TripContext(journal_voice="first_person_plural")
        rendered = context.render()
        assert "we" in rendered.lower()

    def test_rendered_block_contains_known_plans(self) -> None:
        context = TripContext(known_plans=("Mozart concert, 20:15",))
        rendered = context.render()
        assert "Mozart concert, 20:15" in rendered

    def test_rendered_block_contains_notes(self) -> None:
        context = TripContext(notes=("It mattered because of the view.",))
        rendered = context.render()
        assert "It mattered because of the view." in rendered

    def test_rendered_block_contains_roles_only_traveler(self) -> None:
        context = TripContext(travelers=(Traveler(role="child"),))
        rendered = context.render()
        assert "child" in rendered

    def test_empty_context_renders_absent_block(self) -> None:
        assert TripContext().render() == TripContext.render_absent()

    def test_absent_block_tells_model_to_stay_factual(self) -> None:
        rendered = TripContext.render_absent()
        assert "factual" in rendered.lower()
        assert "do not invent" in rendered.lower()
