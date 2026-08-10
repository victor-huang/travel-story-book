"""The service skeleton: two endpoints, and nothing that presumes a hosting target.

S01 exists to settle where the service's code lives, what it runs, and how a developer runs it --
so S02-S07 have somewhere to write. It deliberately contains **no** object-store client, no queue
and no auth: each of those is determined by a hosting decision that has not been taken (see the
iOS tracker's open questions 14-18). Building an adapter against a guess is the expensive kind of
wrong here, because six tasks would inherit it.

`/health` and `/ready` are separate because they answer different questions and a load balancer
needs the first to keep answering while the second says no.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Response

from storybook_service.capability import Report, probe
from storybook_service.settings import Settings

SERVICE_NAME = "storybook-service"


def _serialise(report: Report) -> dict[str, Any]:
    return {
        "ready": report.ready,
        "measured_at": report.measured_at.isoformat(),
        "checks": [asdict(check) for check in report.checks],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Settings pinned before startup win, so a caller can point the probe at another binary. Reading
    # the environment here unconditionally would silently discard that.
    settings = getattr(app.state, "settings", None) or Settings.from_env()
    app.state.settings = settings
    # Probed once, at startup, and the response carries `measured_at` so a reader is never told a
    # cached reading is a fresh one.
    app.state.capability = probe(settings)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness, and it claims nothing more.

        Answering this proves the process is up. It does not prove exiftool is installed or that a
        build could run, and saying so here is cheaper than someone inferring it.
        """
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "scope": "liveness",
            "detail": "the process is answering; dependencies are reported by /ready",
        }

    @app.get("/ready")
    def ready(response: Response) -> dict[str, Any]:
        """Readiness, with the dependency measurements behind it.

        503 when a required dependency is missing, because a service that cannot read EXIF cannot
        produce a trip -- every timestamp, day boundary and timezone comes from it. Optional
        dependencies are reported and never gate: the pipeline degrades rather than aborting, and
        the caller is told exactly what degraded.
        """
        report: Report = app.state.capability
        if not report.ready:
            response.status_code = 503
        return _serialise(report)

    return app


app = create_app()
