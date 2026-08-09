"""Unit tests for the trip scaffold. No filesystem beyond tmp_path, no media."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from story_book.init_trip import (
    EXAMPLE_CONFIG,
    InitError,
    Setting,
    face_detector_setting,
    home_settings,
    next_steps,
    plan_settings,
    render_config,
    resolve_face_model,
    write_trip_dir,
)
from story_book.profile import Profile

EXAMPLE = """\
config_version = 1
# trip_name = "Europe 2026"

# [home]
# lat = 37.7749
# lon = -122.4194

[time]
day_start_hour = 4
default_timezone = "UTC"

[models]
# face_detector_model = "models/yunet.onnx"
"""


def parsed(text: str) -> dict:
    return tomllib.loads(text)


class TestRenderConfig:
    def test_replaces_an_uncommented_value(self) -> None:
        out = render_config(EXAMPLE, {"time.day_start_hour": Setting("5")})
        assert parsed(out)["time"]["day_start_hour"] == 5

    def test_uncomments_a_commented_key(self) -> None:
        out = render_config(EXAMPLE, {"trip_name": Setting('"Japan 2027"')})
        assert parsed(out)["trip_name"] == "Japan 2027"

    def test_uncomments_a_commented_table_when_it_gains_a_value(self) -> None:
        out = render_config(EXAMPLE, {"home.lat": Setting("1.5"), "home.lon": Setting("2.5")})
        assert parsed(out)["home"] == {"lat": 1.5, "lon": 2.5}

    def test_leaves_a_commented_table_commented_when_it_gains_nothing(self) -> None:
        out = render_config(EXAMPLE, {"time.day_start_hour": Setting("5")})
        assert "home" not in parsed(out)

    def test_keeps_the_explanatory_comments(self) -> None:
        source = "[time]\n# why this exists\nday_start_hour = 4\n"
        out = render_config(source, {"time.day_start_hour": Setting("5")})
        assert "# why this exists" in out

    def test_writes_the_basis_beside_the_value(self) -> None:
        out = render_config(EXAMPLE, {"time.day_start_hour": Setting("5", "17 late-night items")})
        assert "# measured: 17 late-night items" in out

    def test_an_inherited_value_is_not_labelled_measured(self) -> None:
        out = render_config(
            EXAMPLE, {"trip_name": Setting('"J"', "from last year", origin="copied")}
        )
        assert "# copied: from last year" in out and "measured" not in out

    def test_the_header_replaces_the_examples_copy_me_banner(self) -> None:
        out = render_config(EXAMPLE, {}, header="# generated\n")
        assert out.startswith("# generated\n") and "Copy to config.toml" not in out

    def test_the_header_does_not_break_the_toml(self) -> None:
        out = render_config(EXAMPLE, {"time.day_start_hour": Setting("5")}, header="# generated\n")
        assert parsed(out)["time"]["day_start_hour"] == 5

    def test_an_unknown_key_is_an_error_not_a_silent_omission(self) -> None:
        with pytest.raises(InitError, match="no key"):
            render_config(EXAMPLE, {"time.invented_key": Setting("1")})

    def test_a_key_in_the_wrong_table_is_not_matched(self) -> None:
        with pytest.raises(InitError, match="models.day_start_hour"):
            render_config(EXAMPLE, {"models.day_start_hour": Setting("5")})

    def test_output_is_valid_toml_with_no_settings(self) -> None:
        assert parsed(render_config(EXAMPLE, {})) == parsed(EXAMPLE)

    def test_the_shipped_example_round_trips(self) -> None:
        text = EXAMPLE_CONFIG.read_text()
        assert parsed(render_config(text, {})) == parsed(text)


class TestHomeSettings:
    def test_copies_every_home_field(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[home]\nlat = 1.0\nlon = 2.0\nexclusion_km = 3.0\n")
        assert set(home_settings(path)) == {"home.lat", "home.lon", "home.exclusion_km"}

    def test_preserves_the_value_exactly(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[home]\nlat = 37.268134\n")
        assert home_settings(path)["home.lat"].value == "37.268134"

    def test_a_config_without_home_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[time]\nday_start_hour = 4\n")
        with pytest.raises(InitError, match="no \\[home\\] block"):
            home_settings(path)

    def test_invalid_toml_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[home\n")
        with pytest.raises(InitError, match="not valid TOML"):
            home_settings(path)


class TestFaceDetectorSetting:
    def test_writes_an_absolute_path(self, tmp_path: Path) -> None:
        model = tmp_path / "yunet.onnx"
        model.write_bytes(b"x")
        value = face_detector_setting(model, basis="test")["models.face_detector_model"].value
        assert Path(value.strip('"')).is_absolute()

    def test_a_missing_model_fails_at_scaffold_time(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            face_detector_setting(tmp_path / "absent.onnx", basis="test")

    def test_the_error_says_how_to_get_one(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="opencv_zoo"):
            face_detector_setting(tmp_path / "absent.onnx", basis="test")


class TestResolveFaceModel:
    def test_an_absolute_path_passes_through(self, tmp_path: Path) -> None:
        assert resolve_face_model(str(tmp_path / "m.onnx"), relative_to=tmp_path).is_absolute()

    def test_a_relative_path_resolves_beside_the_source_config(self, tmp_path: Path) -> None:
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "m.onnx").write_bytes(b"x")
        assert resolve_face_model("models/m.onnx", relative_to=tmp_path).is_file()

    def test_an_unresolvable_relative_path_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="relative"):
            resolve_face_model("models/absent.onnx", relative_to=tmp_path)


class TestPlanSettings:
    def test_an_empty_profile_still_names_the_trip(self) -> None:
        settings = plan_settings(Profile(source=Path("/s")), trip_name="Japan 2027")
        assert settings["trip_name"].value == '"Japan 2027"'

    def test_an_empty_profile_writes_no_thresholds(self) -> None:
        settings = plan_settings(Profile(source=Path("/s")))
        assert settings == {}

    def test_inherited_home_is_included(self) -> None:
        home = {"home.lat": Setting("1.0")}
        assert "home.lat" in plan_settings(Profile(source=Path("/s")), home=home)


class TestWriteTripDir:
    def test_creates_both_files(self, tmp_path: Path) -> None:
        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        assert plan.config_path.is_file() and plan.overrides_path.is_file()

    def test_refuses_to_overwrite_an_existing_config(self, tmp_path: Path) -> None:
        write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        with pytest.raises(InitError, match="already exists"):
            write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))

    def test_keeps_corrections_a_person_already_wrote(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        trip.mkdir()
        (trip / "overrides.toml").write_text("# mine\n")
        plan = write_trip_dir(trip, {}, source=Path("/src"))
        assert plan.overrides_path.read_text() == "# mine\n" and plan.overrides_existed

    def test_the_written_config_loads(self, tmp_path: Path) -> None:
        from story_book.config import Config

        plan = write_trip_dir(
            tmp_path / "trip", {"time.day_start_hour": Setting("5")}, source=Path("/src")
        )
        assert Config.load(plan.config_path).time.day_start_hour == 5


class TestStarterOverrides:
    """The example names files from the trip it was written for. Copied verbatim, it fails a run."""

    def test_it_selects_nothing(self, tmp_path: Path) -> None:
        from story_book.overrides import Overrides

        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        loaded = Overrides.load(plan.overrides_path)
        assert loaded.is_empty

    def test_no_example_filename_survives_uncommented(self, tmp_path: Path) -> None:
        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        live = [
            line
            for line in plan.overrides_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert live == ["override_version = 1"]

    def test_the_guidance_comments_survive(self, tmp_path: Path) -> None:
        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        assert "asset id" in plan.overrides_path.read_text()


class TestNextSteps:
    def test_every_step_names_the_config(self, tmp_path: Path) -> None:
        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        steps = next_steps(Path("/src"), plan)
        assert all("--config" in step for step in steps if step.startswith("story-book"))

    def test_build_is_not_run_for_you_but_is_the_first_step(self, tmp_path: Path) -> None:
        plan = write_trip_dir(tmp_path / "trip", {}, source=Path("/src"))
        assert next_steps(Path("/src"), plan)[0].startswith("story-book build /src")
