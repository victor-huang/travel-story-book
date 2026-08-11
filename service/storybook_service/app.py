"""The app: health, readiness, and ingest.

S01 settled where the code lives, what it runs and how a developer runs it, and deliberately built
no object-store client, no queue and no auth. S02 adds the first of those -- S3 was ratified as the
object store -- and still builds **no queue** (S03) and **no auth** (S06); `principal.py` says
plainly that today's caller is believed rather than verified.

`/health` and `/ready` are separate because they answer different questions and a load balancer
needs the first to keep answering while the second says no. The index and the object store are
constructor parameters for the same reason: which engine holds the index is undecided, and a test
substitutes a local fake for S3.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Response

from storybook_service import index as index_module
from storybook_service.capability import Report, probe
from storybook_service.ingest import router as ingest_router
from storybook_service.jobs import router as jobs_router
from storybook_service.objectstore import ObjectStoreError, S3ObjectStore
from storybook_service.principal import DEV_IDENTITY_HEADER
from storybook_service.settings import Settings
from storybook_service.worker import start_inline_worker, stop_inline_worker

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

    # Said out loud at every start, because it is the kind of gap that gets forgotten between the
    # session that opened it and the deployment that closes it. S06 is the fix.
    logging.getLogger(SERVICE_NAME).warning(
        "authentication is not implemented: the %s header is believed as sent, and any caller may "
        "act as any user. S06 (Google, then Apple) replaces it. Not for the public internet.",
        DEV_IDENTITY_HEADER,
    )

    owns_index = getattr(app.state, "index", None) is None
    if owns_index:
        app.state.index = index_module.for_dsn(settings.resolved_index_dsn())
    if getattr(app.state, "object_store", None) is None:
        try:
            app.state.object_store = S3ObjectStore(settings)
        except ObjectStoreError:
            # An unconfigured bucket is not a reason to refuse to start: /health, /ready and the
            # trip list all still answer, and the ingest routes say 503 with what to set. The
            # bucket does not exist yet (open question 15), so this is today's normal state.
            app.state.object_store = None
    # The worker runs here by default (S03, question 16). A separate `python -m
    # storybook_service.worker` process is supported and equivalent -- the claim is one transaction
    # either way -- but the default has to be the one whose failure is loud: a service with no
    # worker answers /health with 200 and never builds anything.
    app.state.worker = None
    if settings.worker_inline:
        start_inline_worker(app.state)

    try:
        yield
    finally:
        stop_inline_worker(app.state)
        if owns_index:
            app.state.index.close()


def create_app(
    settings: Settings | None = None,
    *,
    index: Any | None = None,
    object_store: Any | None = None,
) -> FastAPI:
    """Build the app, with the two collaborators injectable.

    They are parameters because both are decisions a human has not taken: which engine holds the
    index (Postgres on RDS vs. SQLite on EBS) and, in tests, a fake S3. Anything pinned here wins
    over the environment, the same rule `lifespan` already applies to settings.
    """
    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings
    app.state.index = index
    app.state.object_store = object_store
    app.include_router(ingest_router)
    app.include_router(jobs_router)

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
