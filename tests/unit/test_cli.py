from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from story_book import __version__
from story_book.cli import _overrides_path, _story_dir_file, app

runner = CliRunner()


@pytest.fixture
def empty_source(tmp_path: Path) -> Path:
    path = tmp_path / "trip"
    path.mkdir()
    return path


class TestTopLevel:
    def test_bare_invocation_shows_help(self) -> None:
        assert "Usage" in runner.invoke(app, []).output

    def test_version_is_reported(self) -> None:
        assert __version__ in runner.invoke(app, ["--version"]).output

    def test_commands_are_listed_in_help(self) -> None:
        output = runner.invoke(app, ["--help"]).output
        assert "build" in output and "report" in output and "profile" in output


class TestBuild:
    def test_it_succeeds_on_an_empty_source(self, empty_source: Path, tmp_path: Path) -> None:
        result = runner.invoke(app, ["build", str(empty_source), "--out", str(tmp_path / "out")])
        assert result.exit_code == 0

    def test_it_creates_the_database(self, empty_source: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        runner.invoke(app, ["build", str(empty_source), "--out", str(out)])
        assert (out / "story.db").exists()

    def test_the_trip_name_defaults_to_the_folder_name(
        self, empty_source: Path, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["build", str(empty_source), "--out", str(tmp_path / "o")])
        assert "trip" in result.output

    def test_a_missing_source_is_rejected(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["build", str(tmp_path / "absent"), "--out", str(tmp_path)])
        assert result.exit_code != 0

    def test_a_file_as_source_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "photo.jpg"
        target.write_bytes(b"x")
        result = runner.invoke(app, ["build", str(target), "--out", str(tmp_path / "o")])
        assert result.exit_code != 0

    def test_an_invalid_transcribe_mode_is_rejected(
        self, empty_source: Path, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app,
            ["build", str(empty_source), "--out", str(tmp_path / "o"), "--transcribe", "maybe"],
        )
        assert result.exit_code == 2

    def test_a_bad_config_exits_with_a_config_error(
        self, empty_source: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "config.toml"
        config.write_text("nonsense_key = 1\n")
        result = runner.invoke(
            app, ["build", str(empty_source), "--out", str(tmp_path / "o"), "-c", str(config)]
        )
        assert result.exit_code == 2

    def test_a_valid_config_is_accepted(self, empty_source: Path, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('trip_name = "Europe 2026"\n')
        result = runner.invoke(
            app, ["build", str(empty_source), "--out", str(tmp_path / "o"), "-c", str(config)]
        )
        assert "Europe 2026" in result.output

    def test_dry_run_is_accepted(self, empty_source: Path, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["build", str(empty_source), "--out", str(tmp_path / "o"), "--dry-run"]
        )
        assert result.exit_code == 0

    def test_no_cloud_is_accepted(self, empty_source: Path, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["build", str(empty_source), "--out", str(tmp_path / "o"), "--no-cloud"]
        )
        assert result.exit_code == 0


class TestReport:
    def test_a_missing_database_is_reported(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["report", "--out", str(tmp_path / "nothing")])
        assert result.exit_code == 2

    def test_it_opens_an_existing_database(self, empty_source: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        runner.invoke(app, ["build", str(empty_source), "--out", str(out)])
        assert runner.invoke(app, ["report", "--out", str(out)]).exit_code == 0


class TestProfile:
    def test_it_accepts_a_source_folder(self, empty_source: Path) -> None:
        assert runner.invoke(app, ["profile", str(empty_source)]).exit_code == 0

    def test_a_missing_folder_is_rejected(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["profile", str(tmp_path / "absent")]).exit_code != 0


class TestProfileOutput:
    def test_it_reports_media_counts(self, media_dir: Path) -> None:
        result = runner.invoke(app, ["profile", str(media_dir)])
        assert "Media" in result.output

    def test_it_writes_json_when_asked(self, media_dir: Path, tmp_path: Path) -> None:
        target = tmp_path / "profile.json"
        runner.invoke(app, ["profile", str(media_dir), "--json", str(target)])
        assert target.exists()

    def test_the_json_is_valid(self, media_dir: Path, tmp_path: Path) -> None:
        target = tmp_path / "profile.json"
        runner.invoke(app, ["profile", str(media_dir), "--json", str(target)])
        assert json.loads(target.read_text())["media"]["total"] > 0


class TestOverridesDiscovery:
    """Where `build` looks for corrections when `--overrides` is not given.

    Anchored to the config, never to the current directory: a stray `overrides.toml` in a
    checkout must not silently apply itself to an unrelated trip. This was a real bug -- the
    repo's own overrides file leaked into the CLI tests.
    """

    def test_nothing_is_found_without_a_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "overrides.toml").write_text('pin = ["IMG_1.jpeg"]\n')

        assert _overrides_path(None, None) is None

    def test_a_file_beside_the_config_is_found(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("")
        beside = tmp_path / "overrides.toml"
        beside.write_text('pin = ["IMG_1.jpeg"]\n')

        assert _overrides_path(None, config) == beside

    def test_an_absent_file_beside_the_config_is_not_an_error(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text("")

        assert _overrides_path(None, config) is None

    def test_an_explicit_missing_file_exits_rather_than_being_ignored(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit):
            _overrides_path(tmp_path / "absent.toml", None)


class TestStoryDirectoryDiscovery:
    """`<out>/story/` holds what a chat returned. Anchored to --out, never to the cwd."""

    def test_story_json_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "story").mkdir()
        target = tmp_path / "story" / "story.json"
        target.write_text("{}")

        assert _story_dir_file(tmp_path, "story.json") == target

    def test_a_missing_file_is_none_not_a_guess(self, tmp_path: Path) -> None:
        assert _story_dir_file(tmp_path, "story.json") is None

    def test_toml_wins_over_yaml_when_both_exist(self, tmp_path: Path) -> None:
        (tmp_path / "story").mkdir()
        (tmp_path / "story" / "trip_context.toml").write_text("")
        (tmp_path / "story" / "trip_context.yaml").write_text("")

        found = _story_dir_file(
            tmp_path, "trip_context.toml", "trip_context.yaml", "trip_context.yml"
        )
        assert found.name == "trip_context.toml"

    def test_yaml_is_found_when_there_is_no_toml(self, tmp_path: Path) -> None:
        (tmp_path / "story").mkdir()
        (tmp_path / "story" / "trip_context.yaml").write_text("")

        found = _story_dir_file(
            tmp_path, "trip_context.toml", "trip_context.yaml", "trip_context.yml"
        )
        assert found.name == "trip_context.yaml"
