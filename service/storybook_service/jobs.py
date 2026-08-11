"""The job routes: queue a build, and report on one honestly.

The wire contract, which `JobPoller` (I22) is the consumer of:

    POST /trips/{trip_id}/build     {}   -> 202 {job_id, state, created: true, ...}
                                         -> 200 {job_id, state, created: false, ...} when this
                                            trip already has a job queued or running
    GET  /trips/{trip_id}/jobs           -> 200 {jobs: [...]}   newest first
    GET  /jobs/{job_id}                  -> 200 {state, stage, done, total, ...}

`{state, stage, done, total}` is exactly what the tracker's I22 entry asks for, and everything else
in the body is there because one of these four cannot carry it:

- **`done` and `total` are per stage**, because that is the granularity the pipeline records. There
  is deliberately **no percentage**: eighteen stages cost wildly different amounts per item, so one
  number over all of them would be an invention. `stage_index` and `stages_total` are the honest
  way to show overall position, and both are facts.
- **`total` may be `null`.** It is null when nothing has been measured yet (a queued job) or when a
  stage's candidate set could not be counted. A substituted `0` would read as "nothing to do".
- **`degraded` and `unavailable_stages`** answer "was this build as good as this pipeline gets?"
  in the words of the stage that stopped working. Without the `clip` extra a build succeeds and
  dedup loses its semantic half; a job that does not say so is a job overstating its result.
- **`error` is the build's own last words**, not a paraphrase. A failed job must not read like a
  finished one.

`GET /jobs/{job_id}` is scoped by owner **in the query** (`Index.get_job` cannot be called without
an `owner_id`), and a job belonging to someone else is a 404 identical to one that does not exist --
otherwise a caller could enumerate job ids.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from storybook_service.index import Index, Job
from storybook_service.index_sqlite import new_id
from storybook_service.principal import Principal, resolve_principal
from storybook_service.progress import BuildProgress, read_build_progress
from storybook_service.settings import Settings
from storybook_service.source import trip_paths

router = APIRouter()


class BuildRequest(BaseModel):
    """Deliberately empty.

    `ios_backend_service.md` shows `POST /trips/{id}/build {config, overrides}`. Neither is accepted
    here, and the omission is a decision rather than an oversight: **who owns the config is open
    question 8**, and the config this build uses is the one `story-book init` *measured* from the
    uploaded media. Accepting a client-supplied config would replace measurements with whatever the
    phone last cached, permanently, since `init` will not overwrite its own file. Overrides belong
    to I43, and there is no route that writes them yet.
    """


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _index(request: Request) -> Index:
    return request.app.state.index


PrincipalDep = Annotated[Principal, Depends(resolve_principal)]
SettingsDep = Annotated[Settings, Depends(_settings)]
IndexDep = Annotated[Index, Depends(_index)]


def _capability_json(job: Job) -> dict[str, Any]:
    if not job.capability:
        return {}
    try:
        return json.loads(job.capability)
    except json.JSONDecodeError:  # pragma: no cover - written by this codebase only
        return {}


def _prepare_progress(index: Index, job: Job, settings: Settings) -> tuple[int, int]:
    """Real counts for the fetch step: files on disk at their declared length, over declared.

    The same test `materialise_source` applies -- size, not existence -- so a partially fetched
    object is not counted as arrived.
    """
    paths = trip_paths(settings, job.trip_id)
    assets = index.trip_assets(trip_id=job.trip_id)
    present = 0
    for asset in assets:
        candidate = paths.source / asset.stored_filename
        if candidate.exists() and candidate.stat().st_size == asset.size:
            present += 1
    return present, len(assets)


def _job_json(index: Index, job: Job, settings: Settings) -> dict[str, Any]:
    capability = _capability_json(job)
    body: dict[str, Any] = {
        "job_id": job.id,
        "trip_id": job.trip_id,
        "kind": job.kind,
        "state": job.state,
        "phase": job.phase or None,
        "attempts": job.attempts,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "error": job.error or None,
        "exit_code": job.exit_code,
        "degraded": capability.get("degraded"),
        "unavailable_stages": capability.get("unavailable", []),
        "capability_measured_at": capability.get("measured_at"),
    }

    if job.state == "queued":
        ahead = index.queued_ahead(job_id=job.id)
        body |= {
            "stage": None,
            "done": 0,
            "total": None,
            "stage_index": None,
            "stages_total": None,
            "queued_ahead": ahead,
            "progress_basis": (
                "queued: no work has started, so there is nothing measured. `total` is null rather "
                "than 0 because the size of the work is not known yet, and `queued_ahead` is a "
                "count of jobs, never an estimated wait."
            ),
        }
        return body

    if job.phase == "prepare":
        present, declared = _prepare_progress(index, job, settings)
        body |= {
            "stage": "source:prepare",
            "done": present,
            "total": declared,
            "stage_index": None,
            "stages_total": None,
            "progress_basis": (
                "fetching the uploaded media: files present at their declared length, over assets "
                "declared. Length rather than existence, so a half-fetched object is not counted."
            ),
        }
        return body

    paths = trip_paths(settings, job.trip_id)
    progress: BuildProgress = read_build_progress(
        out_dir=paths.out,
        source_dir=paths.source,
        config_path=paths.config,
        capability=job.capability,
    )
    body |= {
        "stage": progress.stage,
        "done": progress.done,
        "total": progress.total,
        "stage_index": progress.stage_index,
        "stages_total": progress.stages_total,
        "stages_complete": progress.stages_complete,
        "media_known": progress.media_known,
        "progress_basis": progress.basis,
        "progress_detail": progress.detail or None,
        "stages": [
            {
                "name": stage.name,
                "version": stage.version,
                "state": stage.state,
                "done": stage.done,
                "failed": stage.failed,
                "total": stage.total,
                "detail": stage.detail or None,
            }
            for stage in progress.stages
        ],
    }
    if job.state == "succeeded":
        body["trip_json"] = str(paths.out / "trip.json")
    return body


@router.post("/trips/{trip_id}/build")
def start_build(
    trip_id: str,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
    response: Response,
    body: BuildRequest | None = None,
) -> dict[str, Any]:
    """Queue a build of this trip. **The unit of work is the trip, and there is no other unit.**

    T58 is why this route takes no day, range or subset: a package split per day came back as three
    one-day `story.json` files. Uploads are chunked per asset; the work is not.

    A second request while one is queued or running returns **that** job with `created: false` and
    `200`. It is not an error: the client that lost its `job_id` to a relaunch needs it back, and
    `story.db` has a single-row `trip` table, so two concurrent builds of one trip are not a shape
    the pipeline supports.
    """
    trip = index.get_trip(owner_id=principal.user_id, trip_id=trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail=f"no trip {trip_id!r}")

    job, created = index.enqueue_job(
        owner_id=principal.user_id, trip_id=trip.id, kind="build", job_id=new_id()
    )
    response.status_code = 202 if created else 200
    return {
        **_job_json(index, job, settings),
        "created": created,
        "detail": (
            "queued"
            if created
            else "this trip already has a job queued or running; that job is returned rather than "
            "a second one started, because one story.db cannot be built twice at once"
        ),
    }


@router.get("/trips/{trip_id}/jobs")
def list_jobs(
    trip_id: str, principal: PrincipalDep, index: IndexDep, settings: SettingsDep
) -> dict[str, Any]:
    """Every job for this trip, newest first.

    Not in the design doc's endpoint list, and it exists for one concrete case: the app is killed
    mid-build and comes back without the `job_id` it was polling. Without this it would have to
    start a second build to discover the first.
    """
    trip = index.get_trip(owner_id=principal.user_id, trip_id=trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail=f"no trip {trip_id!r}")
    jobs = index.list_jobs(owner_id=principal.user_id, trip_id=trip.id)
    return {"trip_id": trip.id, "jobs": [_job_json(index, job, settings) for job in jobs]}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str, principal: PrincipalDep, index: IndexDep, settings: SettingsDep
) -> dict[str, Any]:
    job = index.get_job(owner_id=principal.user_id, job_id=job_id)
    if job is None:
        # Identical to "no such job". Someone else's job id must not be distinguishable from a
        # made-up one, or the 403/404 difference becomes an enumeration oracle.
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return _job_json(index, job, settings)
