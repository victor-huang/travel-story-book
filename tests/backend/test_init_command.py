"""`story-book init` against the committed fixture media.

The unit tests prove the rewriter; these prove the command produces a config that the pipeline
actually accepts, from a folder of real files rather than a synthesized profile.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from story_book.cli import app
from story_book.config import Config
from story_book.init_trip import EXAMPLE_CONFIG, EXAMPLE_OVERRIDES
from story_book.profile import run as run_profile
from story_book.profile import suggestions

FIXTURES = Path(__file__).parents[1] / "fixtures" / "media"
runner = CliRunner()


def init(*args: str) -> object:
    return runner.invoke(app, ["init", str(FIXTURES), *args])


def config_of(trip_dir: Path) -> Config:
    return Config.load(trip_dir / "config.toml")


class TestInitCommand:
    def test_fixtures_are_present(self) -> None:
        assert FIXTURES.is_dir() and any(FIXTURES.iterdir())

    def test_writes_a_config_the_pipeline_loads(self, tmp_path: Path) -> None:
        result = init("--trip-dir", str(tmp_path / "trip"), "--no-face-model")
        assert result.exit_code == 0, result.output
        assert config_of(tmp_path / "trip").time.day_start_hour == 4

    def test_the_trip_name_defaults_to_the_folder(self, tmp_path: Path) -> None:
        init("--trip-dir", str(tmp_path / "trip"), "--no-face-model")
        assert config_of(tmp_path / "trip").trip_name == FIXTURES.name

    def test_measured_values_reach_the_file(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        written = tomllib.loads((trip / "config.toml").read_text())
        for key, _, _ in suggestions(run_profile(FIXTURES)):
            section, name = key.split(".")
            assert name in written[section], key

    def test_every_measured_value_carries_its_basis(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        measured = sum(
            1
            for line in (trip / "config.toml").read_text().splitlines()
            if line.startswith("# measured:")
        )
        assert measured >= len(suggestions(run_profile(FIXTURES)))

    def test_an_inherited_coordinate_is_not_called_a_measurement(self, tmp_path: Path) -> None:
        like = tmp_path / "first.toml"
        like.write_text("config_version = 1\n[home]\nlat = 1.5\nlon = -2.5\nexclusion_km = 3.0\n")
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--like", str(like), "--no-face-model")
        home_block = (trip / "config.toml").read_text().split("[time]")[0]
        assert "# copied:" in home_block and "# measured:" not in home_block

    def test_the_banner_says_the_file_was_generated(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        text = (trip / "config.toml").read_text()
        assert text.startswith("# Travel Story Book configuration for ")
        assert "Copy to config.toml" not in text

    def test_the_documentation_survives(self, tmp_path: Path) -> None:
        """A config with the comments stripped is a config nobody can safely edit."""
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        assert "Media within exclusion_km" in (trip / "config.toml").read_text()

    def test_home_is_inherited_from_another_trip(self, tmp_path: Path) -> None:
        like = tmp_path / "first.toml"
        like.write_text("config_version = 1\n[home]\nlat = 1.5\nlon = -2.5\nexclusion_km = 3.0\n")
        init("--trip-dir", str(tmp_path / "trip"), "--like", str(like), "--no-face-model")
        home = config_of(tmp_path / "trip").home
        assert (home.lat, home.lon, home.exclusion_km) == (1.5, -2.5, 3.0)

    def test_without_home_the_privacy_gap_is_stated(self, tmp_path: Path) -> None:
        result = init("--trip-dir", str(tmp_path / "trip"), "--no-face-model")
        assert "no [home] block" in result.output

    def test_without_a_face_model_the_quality_gap_is_stated(self, tmp_path: Path) -> None:
        result = init("--trip-dir", str(tmp_path / "trip"), "--no-face-model")
        assert "no face detector" in result.output

    def test_a_face_model_is_written_as_an_absolute_path(self, tmp_path: Path) -> None:
        model = tmp_path / "yunet.onnx"
        model.write_bytes(b"x")
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--face-model", str(model))
        written = config_of(trip).models.face_detector_model
        assert Path(written).is_absolute() and Path(written).is_file()

    def test_a_missing_face_model_stops_before_writing_anything(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        result = init("--trip-dir", str(trip), "--face-model", str(tmp_path / "absent.onnx"))
        assert result.exit_code == 2 and not trip.exists()

    def test_it_refuses_to_overwrite(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        result = init("--trip-dir", str(trip), "--no-face-model")
        assert result.exit_code == 2 and "already exists" in result.output

    def test_it_does_not_build(self, tmp_path: Path) -> None:
        trip = tmp_path / "trip"
        init("--trip-dir", str(trip), "--no-face-model")
        assert not (trip / "out").exists()

    def test_the_help_does_not_lose_the_word_home_to_rich_markup(self) -> None:
        """Rich reads a bracketed literal as a style tag and silently drops it."""
        assert "[home]" in runner.invoke(app, ["init", "--help"]).output

    def test_it_prints_the_build_command(self, tmp_path: Path) -> None:
        result = init("--trip-dir", str(tmp_path / "trip"), "--no-face-model")
        assert "story-book build" in result.output


class TestExamplesHaveNotDrifted:
    """Two copies of one file is one copy eventually wrong."""

    def test_config_example(self) -> None:
        assert Path("config.example.toml").read_text() == EXAMPLE_CONFIG.read_text()

    def test_overrides_example(self) -> None:
        assert Path("overrides.example.toml").read_text() == EXAMPLE_OVERRIDES.read_text()


class TestSuggestionsAreWritable:
    def test_every_suggested_key_exists_in_the_example_config(self) -> None:
        """A suggestion init cannot write is a measurement that silently evaporates."""
        example = EXAMPLE_CONFIG.read_text()
        for key, _, _ in suggestions(run_profile(FIXTURES)):
            _, name = key.split(".")
            assert f"{name} =" in example, key
