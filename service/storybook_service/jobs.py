"""The job routes: queue a build or a reel, and report on either honestly.

The wire contract, which `JobPoller` (I22) is the consumer of:

    POST /trips/{trip_id}/build     {}   -> 202 {job_id, state, created: true, ...}
                                         -> 200 {job_id, state, created: false, ...} when this
                                            trip already has a job queued or running
    POST /trips/{trip_id}/reel      {...options...} -> 202/200, same shape as build
    GET  /trips/{trip_id}/jobs           -> 200 {jobs: [...]}   newest first
    GET  /jobs/{job_id}                  -> 200 {state, stage, done, total, kind, ...}

S07 adds the second route. It reuses the queue, the worker, the state machine and this same
`GET /jobs/{job_id}` wholesale -- a reel is a second job **kind**, not a second mechanism. The one
real difference is what `stage`/`done`/`total` are read from while `state == "running"`: a build's
come from `story.db`, live (`progress.py`); a reel touches no database, so its own segment plan
and cache-file counts are what `reel_jobs.py` reads instead. Both publish the same four fields.

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
from pydantic import BaseModel, Field, field_validator

from story_book.export.reel import ReelError, parse_aspect
from storybook_service.index import Index, Job
from storybook_service.index_sqlite import new_id
from storybook_service.naming import NamingError, validate_hash
from storybook_service.principal import Principal, resolve_principal
from storybook_service.progress import BuildProgress, read_build_progress
from storybook_service.reel_jobs import reel_render_progress
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


class ReelRequest(BaseModel):
    """What I30 (`ReelOptions.swift`) offers, unlike a build's empty request -- there can be many
    reels for one trip (D5, a re-cut is a new job, never a mutation), so each needs its own record
    of what was asked for rather than one config the trip measures once.

    `music_hash` names an asset the client already negotiated and uploaded through the ordinary
    ingest routes (S02) -- **there is no separate upload path for music**, per the task: a track
    is just another hash-addressed asset of this trip. It must already be declared on this trip or
    the request is refused before a job is even queued, rather than failing later inside the
    worker where the only reader is a log.
    """

    aspect: str | None = Field(default=None, description='e.g. "16:9" or "9:16"; default 16:9')
    music_hash: str | None = Field(
        default=None, description="hash of an already-negotiated, already-uploaded asset"
    )
    day: str | None = Field(default=None, description="YYYY-MM-DD; render one day only")
    date_from: str | None = Field(default=None, description="YYYY-MM-DD, inclusive")
    date_to: str | None = Field(default=None, description="YYYY-MM-DD, inclusive")
    places: list[str] = Field(default_factory=list, description="composes with the day range")
    name: str | None = Field(default=None, description="becomes the filename slug and title card")
    subtitles: list[str] = Field(default_factory=list, description='e.g. ["zh", "en"]')
    burn_in: str | None = Field(
        default=None, description="also write a burned-in copy in this language"
    )
    clip_audio: bool | None = Field(default=None, description="play clips' own sound; default true")

    @field_validator("music_hash")
    @classmethod
    def _music_hash(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            return validate_hash(value)
        except NamingError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("aspect")
    @classmethod
    def _aspect(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parse_aspect(value)
        except ReelError as exc:
            raise ValueError(str(exc)) from exc
        return value


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

    if job.kind == "reel":
        # A reel touches no database, so there is nothing for `read_build_progress` below to read
        # that means anything about *this* job -- it would show the prior build's own, already
        # finished, stage_result rows, which is a real fact about the wrong thing. `reel_jobs.py`
        # reads this job's own segment-cache files instead.
        rendered = reel_render_progress(job.progress)
        body |= {
            "stage": rendered["stage"],
            "done": rendered["done"],
            "total": rendered["total"],
            "stage_index": None,
            "stages_total": None,
            "progress_basis": (
                "count of this reel's own rendered-segment cache files against the exact segment "
                "plan computed before rendering started -- the same plan story-book reel itself "
                "renders. No percentage: segments cost wildly different amounts (a title card is "
                "seconds, a long clip excerpt is not)."
            ),
            "progress_detail": rendered["detail"] or None,
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


@router.post("/trips/{trip_id}/reel")
def start_reel(
    trip_id: str,
    body: ReelRequest,
    principal: PrincipalDep,
    index: IndexDep,
    settings: SettingsDep,
    response: Response,
) -> dict[str, Any]:
    """Queue a reel render of this trip (S07), from a build that must already have succeeded.

    Unlike `POST /trips/{id}/build`, this route's request is not empty and is kept: `Job.options`
    carries it verbatim, so the worker replays exactly what was asked for and `GET /jobs/{id}/reel`
    (delivery) can report it back unchanged. **A re-cut is a new job, never a mutation** (D5) --
    every call here that is not deduplicated by the "one active job per trip" rule below produces
    a fresh `job_id`, and any number of finished reels can coexist for one trip.

    `music_hash`, if given, must already be a declared asset of this trip -- refused here with a
    422 rather than discovered by the worker three steps into a render, where the only reader of
    the failure is a log file.
    """
    trip = index.get_trip(owner_id=principal.user_id, trip_id=trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail=f"no trip {trip_id!r}")

    if body.music_hash is not None:
        known = {asset.media_hash for asset in index.trip_assets(trip_id=trip.id)}
        if body.music_hash not in known:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"music_hash {body.music_hash!r} is not a declared asset of this trip. "
                    "Negotiate and upload it first, the same as any photograph or clip -- there "
                    "is no separate upload path for music."
                ),
            )

    options = {
        "aspect": body.aspect,
        "music_hash": body.music_hash,
        "day": body.day,
        "date_from": body.date_from,
        "date_to": body.date_to,
        "places": body.places,
        "name": body.name,
        "subtitles": body.subtitles,
        "burn_in": body.burn_in,
        "clip_audio": body.clip_audio,
    }
    job, created = index.enqueue_job(
        owner_id=principal.user_id,
        trip_id=trip.id,
        kind="reel",
        job_id=new_id(),
        options=json.dumps(options),
    )
    response.status_code = 202 if created else 200
    return {
        **_job_json(index, job, settings),
        "created": created,
        "detail": (
            "queued"
            if created
            else "this trip already has a job queued or running (a build or a reel); that job is "
            "returned rather than a second one started, since both write under one --out"
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
