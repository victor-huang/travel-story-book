"""Parsing and validation of `overrides.toml`. No DB -- resolution is covered in tests/backend."""

from __future__ import annotations

import pytest

from story_book.overrides import OverrideError, Overrides


class TestOverridesFromDict:
    def test_empty_file_produces_empty_overrides(self):
        assert Overrides.from_dict({}).is_empty

    def test_pin_and_reject_are_read_as_filenames(self):
        overrides = Overrides.from_dict({"pin": ["IMG_1.jpeg"], "reject": ["IMG_2.jpeg"]})

        assert overrides.pin == ("IMG_1.jpeg",)
        assert overrides.reject == ("IMG_2.jpeg",)

    def test_a_populated_file_is_not_empty(self):
        assert not Overrides.from_dict({"pin": ["IMG_1.jpeg"]}).is_empty

    def test_event_label_is_read_as_a_table(self):
        overrides = Overrides.from_dict(
            {"label_event": [{"photo": "IMG_1.jpeg", "label": "The concert"}]}
        )

        assert overrides.label_event[0].photo == "IMG_1.jpeg"
        assert overrides.label_event[0].label == "The concert"

    def test_merge_group_keeps_its_photo_list(self):
        overrides = Overrides.from_dict(
            {"merge_events": [{"photos": ["IMG_1.jpeg", "IMG_2.jpeg"]}]}
        )

        assert overrides.merge_events[0].photos == ("IMG_1.jpeg", "IMG_2.jpeg")

    def test_landmark_rename_is_read(self):
        overrides = Overrides.from_dict(
            {"label_landmark": [{"name": "Stephansdom", "label": "St Stephen's"}]}
        )

        assert overrides.label_landmark[0].name == "Stephansdom"
        assert overrides.label_landmark[0].label == "St Stephen's"


class TestOverridesRejectsBadInput:
    def test_unknown_top_level_key_names_the_valid_ones(self):
        with pytest.raises(OverrideError, match="unknown key"):
            Overrides.from_dict({"pinned": ["IMG_1.jpeg"]})

    def test_unsupported_version_is_refused(self):
        with pytest.raises(OverrideError, match="override_version"):
            Overrides.from_dict({"override_version": 99})

    def test_pin_must_be_a_list_of_strings(self):
        with pytest.raises(OverrideError, match="list of filenames"):
            Overrides.from_dict({"pin": "IMG_1.jpeg"})

    def test_a_photo_cannot_be_pinned_and_rejected_at_once(self):
        with pytest.raises(OverrideError, match="both pinned and rejected"):
            Overrides.from_dict({"pin": ["IMG_1.jpeg"], "reject": ["IMG_1.jpeg"]})

    def test_a_merge_of_one_photo_names_no_second_event(self):
        with pytest.raises(OverrideError, match="at least two photos"):
            Overrides.from_dict({"merge_events": [{"photos": ["IMG_1.jpeg"]}]})

    def test_a_misspelled_table_field_is_reported(self):
        with pytest.raises(OverrideError, match="invalid"):
            Overrides.from_dict({"label_event": [{"photo": "IMG_1.jpeg", "name": "x"}]})


class TestOverridesLoad:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert Overrides.load(tmp_path / "nope.toml").is_empty

    def test_no_path_is_not_an_error(self):
        assert Overrides.load(None).is_empty

    def test_a_real_file_round_trips(self, tmp_path):
        path = tmp_path / "overrides.toml"
        path.write_text('override_version = 1\npin = ["IMG_1.jpeg"]\n')

        assert Overrides.load(path).pin == ("IMG_1.jpeg",)
