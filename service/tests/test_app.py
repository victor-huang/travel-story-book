"""Endpoint tests, driven through the real ASGI app rather than by calling the functions."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.capability import Check, Report
from storybook_service.settings import Settings


def _report(*, ready: bool) -> Report:
    return Report(
        ready=ready,
        checks=(
            Check(
                name="story_book_cli",
                ok=ready,
                detail="story-book 0.1.0" if ready else "not found",
                required=True,
                affects="every build",
            ),
        ),
        measured_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )


class TestHealth:
    def test_health_is_reachable(self):
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200

    def test_health_answers_even_when_a_required_dependency_is_missing(self, mocker):
        """Liveness and readiness are separate, and this is the reason they are.

        A container whose exiftool is missing must keep answering /health -- otherwise the platform
        restarts it forever instead of reporting a broken image.
        """
        mocker.patch("storybook_service.app.probe", return_value=_report(ready=False))
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503

    def test_health_claims_only_liveness(self):
        with TestClient(create_app()) as client:
            assert client.get("/health").json()["scope"] == "liveness"


class TestReady:
    def test_ready_returns_200_when_required_dependencies_are_present(self, mocker):
        mocker.patch("storybook_service.app.probe", return_value=_report(ready=True))
        with TestClient(create_app()) as client:
            assert client.get("/ready").status_code == 200

    def test_ready_returns_503_when_a_required_dependency_is_missing(self, mocker):
        mocker.patch("storybook_service.app.probe", return_value=_report(ready=False))
        with TestClient(create_app()) as client:
            assert client.get("/ready").status_code == 503

    def test_ready_reports_when_the_measurement_was_taken(self, mocker):
        """The probe runs at startup, so the response must not imply a reading taken just now."""
        mocker.patch("storybook_service.app.probe", return_value=_report(ready=True))
        with TestClient(create_app()) as client:
            assert client.get("/ready").json()["measured_at"] == "2026-08-10T12:00:00+00:00"

    def test_ready_names_every_check_and_whether_it_gates(self, mocker):
        mocker.patch("storybook_service.app.probe", return_value=_report(ready=True))
        with TestClient(create_app()) as client:
            check = client.get("/ready").json()["checks"][0]
        assert set(check) == {"name", "ok", "detail", "required", "affects"}


class TestUnauthenticated:
    def test_startup_says_out_loud_that_nobody_is_authenticated(self, caplog):
        """A gap that is only in a docstring is a gap that ships.

        S06 owns the fix; until then every start of this service says so.
        """
        with caplog.at_level("WARNING"), TestClient(create_app()):
            pass
        assert any("authentication is not implemented" in r.message for r in caplog.records)


class TestRealDependencies:
    """The acceptance criterion, unmocked: the CLI runs inside whatever is running the service."""

    def test_the_story_book_cli_is_reachable_from_the_service(self):
        with TestClient(create_app()) as client:
            body = client.get("/ready").json()
        cli = next(c for c in body["checks"] if c["name"] == "story_book_cli")
        assert cli["ok"], cli["detail"]
        assert cli["detail"].startswith("story-book "), cli["detail"]

    def test_a_wrong_binary_name_is_reported_as_a_failure(self):
        """The control for the test above.

        Without it, `story_book_cli.ok` being true proves only that the probe can return true.
        """
        app = create_app(Settings(story_book_bin="story-book-does-not-exist"))
        with TestClient(app) as client:
            body = client.get("/ready").json()
        cli = next(c for c in body["checks"] if c["name"] == "story_book_cli")
        assert not cli["ok"]
        assert body["ready"] is False
