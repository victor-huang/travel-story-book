"""The dependency probe.

These run against the real environment on purpose. A mocked probe would prove the shape of the
report and nothing about the image it is reporting on, which is the whole reason the report exists.
"""

from __future__ import annotations

import pytest
from storybook_service.capability import probe
from storybook_service.settings import ENV_PREFIX, Settings


def _named(report, name):
    return next(check for check in report.checks if check.name == name)


class TestProbe:
    def test_the_cli_version_comes_from_running_the_cli(self):
        check = _named(probe(Settings()), "story_book_cli")
        assert check.ok, check.detail
        assert check.detail.startswith("story-book ")

    def test_an_absent_cli_is_a_failure_with_the_reason_attached(self):
        """The control. `detail` must carry the reason, not an empty string."""
        settings = Settings(story_book_bin="story-book-does-not-exist")
        check = _named(probe(settings), "story_book_cli")
        assert not check.ok
        assert check.detail

    def test_required_checks_are_exactly_the_ones_a_build_cannot_survive(self):
        required = {check.name for check in probe(Settings()).checks if check.required}
        assert required == {"story_book_cli", "exiftool", "ffmpeg"}

    def test_optional_dependencies_are_reported_and_do_not_gate_readiness(self, mocker):
        """A `clip`-less image is a supported deployment: the pipeline degrades, so /ready is 200.

        Shown to hold by making CLIP absent rather than by relying on it already being absent --
        a developer machine has the `vision` extra installed and would pass this vacuously.
        """
        mocker.patch(
            "storybook_service.capability.clip_importable",
            return_value=(False, "CLIP unavailable: missing torch, open_clip"),
        )
        report = probe(Settings())
        assert not _named(report, "clip").ok
        assert report.ready

    def test_every_optional_check_names_what_degrades_without_it(self):
        """An absent dependency that says nothing about its consequence is not a useful report."""
        for check in probe(Settings()).checks:
            assert check.affects, check.name

    def test_a_missing_required_dependency_makes_the_report_unready(self, mocker):
        mocker.patch(
            "storybook_service.capability.ffmpeg_available",
            return_value=(False, "ffmpeg/ffprobe not found on PATH"),
        )
        assert not probe(Settings()).ready

    def test_measured_at_is_timezone_aware(self):
        """Durations and instants in this project are UTC-anchored; a naive one raises later."""
        assert probe(Settings()).measured_at.utcoffset() is not None


class TestSettings:
    def test_defaults_apply_with_an_empty_environment(self):
        assert Settings.from_env({}).story_book_bin == "story-book"

    def test_the_environment_overrides_the_binary(self):
        env = {f"{ENV_PREFIX}STORY_BOOK_BIN": "/opt/bin/story-book"}
        assert Settings.from_env(env).story_book_bin == "/opt/bin/story-book"

    def test_an_unparsable_timeout_is_an_error_rather_than_a_silent_default(self):
        with pytest.raises(ValueError):
            Settings.from_env({f"{ENV_PREFIX}PROBE_TIMEOUT_S": "soon"})
