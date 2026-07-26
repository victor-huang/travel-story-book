from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from story_book import __version__
from story_book.cli import app

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
