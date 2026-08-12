"""The worker: claim a job, prepare the folder, run `story-book build`, report honestly.

**What the queue is (open question 16), and why this shape.** A row in the relational index, claimed
in one transaction, polled by a worker that runs the CLI as a subprocess. No broker. The reasoning,
which a human should ratify rather than inherit:

- The index is already there and already durable (question 19: SQLite on EBS, WAL mandatory), so a
  job table costs one table. Redis, SQS or Celery would add a service to operate, a second store
  that can disagree with the index, and -- for SQS -- at-least-once delivery, which for a
  multi-hour build means designing against duplicate execution rather than preventing it.
- The worker must see the *same filesystem* the build wrote to, because progress is read from
  `story.db` and a resumed build reuses `--out`. On one EC2 instance (question 14) that is free. A
  broker buys distribution this deployment cannot use.
- **This choice expires the moment a second instance exists**, exactly as question 19's answer says
  of the index -- one instance is a stated consequence there, and it is the same consequence here.
  A load balancer in front of two API containers is fine; a second *worker* on another machine is
  not, because the trip directory is local. Nothing here would detect that mistake.

`claim_next_job` is a single transaction and "one active job per trip" is a partial unique index, so
running the worker as a thread inside the API process and running it as a separate process from the
same image are the same code with the same guarantees. The inline thread is the default because a
queued job that nothing ever claims is a silent failure, and a deployment that forgot to start a
worker would look healthy.

**A retry is a resume, and defeating that would be the one unforgivable bug here.** Nothing in this
module deletes or rewrites `--out`. `tests/backend/test_runner.py::TestResumeAfterInterrupt` fires a
real `SIGINT` at the pipeline and asserts that re-invoking recomputes only unfinished work; the
CLI exits `130` when that happens, and this worker treats `130` as *requeue*, not as failure.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from story_book.cli import build_stages
from story_book.db import connection as db
from story_book.pipeline.base import StageContext
from storybook_service.index import Index, Job
from storybook_service.objectstore import ObjectStore, ObjectStoreError
from storybook_service.reel_jobs import reel_argv, reel_output_paths, reel_progress_seed
from storybook_service.settings import Settings
from storybook_service.source import (
    ScaffoldError,
    materialise_source,
    scaffold_trip,
    trip_paths,
)

log = logging.getLogger("storybook-service.worker")

INTERRUPTED_EXIT_CODE = 130
"""What `story-book build` exits when the runner caught a `KeyboardInterrupt`.

`cli.build` raises `typer.Exit(130)` for it, and the pipeline has already committed every finished
item. So it is the one non-zero exit that means "come back and carry on".
"""

FAILED_ITEMS_EXIT_CODE = 1
"""The build ran to the end and some items failed. Not a success, and it says which count failed."""


def worker_identity() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """What one execution decided, so a test can assert on it without reading the database."""

    job_id: str
    state: str
    """`succeeded`, `failed`, or `queued` -- the last meaning it was put back to resume."""
    error: str = ""
    exit_code: int | None = None


def measure_capability(
    *, out_dir: Path, source_dir: Path, config_path: Path, no_cloud: bool = False
) -> dict[str, object]:
    """Ask every stage whether it can run, and record the ones that cannot **in their own words**.

    This is the job-level half of open question 18. `/ready` reports the deployment's dependencies
    at startup; this reports what *this build* could do, on the job, where a client polling the job
    can see it. Without the `clip` extra the answer is a perfectly valid `trip.json` with no CLIP
    clustering in it -- a narrower result, not a failure -- and the only way that stops being a
    silent narrowing is if the artifact says so.
    """
    from story_book.config import Config

    conn = db.connect(out_dir / db.DB_FILENAME)
    try:
        config = Config.load(config_path) if config_path.exists() else Config()
        ctx = StageContext(
            conn=conn,
            config=config,
            out_dir=out_dir,
            source_dir=source_dir,
            # The flag the *build* will actually run with, not just what the config says.
            # `--no-cloud` is a CLI argument that overrides the config, and measuring without it
            # would report `landmarks` as available on a run that skips it -- a job reporting a
            # stage nobody was ever going to run.
            no_cloud=no_cloud or config.no_cloud,
        )
        unavailable = []
        available = []
        for stage in build_stages(ctx):
            ok, reason = stage.available(ctx)
            if ok:
                available.append(stage.name)
            else:
                unavailable.append(
                    {"stage": stage.name, "reason": reason, "affects": stage.description}
                )
    finally:
        conn.close()
    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "available": available,
        "unavailable": unavailable,
        "degraded": bool(unavailable),
        "detail": (
            "measured at this job's start by calling each stage's own available(). An unavailable "
            "stage narrows the result and never aborts the build; the reason is the stage's, "
            "quoted."
        ),
    }


class Worker:
    """One worker. Claims one job at a time and runs it to a terminal state.

    Given an `Index` and an `ObjectStore` rather than building them, so a test drives exactly the
    code the deployment runs against a local S3 and a temporary index.
    """

    def __init__(
        self,
        settings: Settings,
        index: Index,
        store: ObjectStore | None,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.index = index
        self.store = store
        self.worker_id = worker_id or worker_identity()

    # --- the loop ---------------------------------------------------------------------------

    def run_forever(self, stop: threading.Event) -> None:
        log.info(
            "worker %s polling for jobs every %ss", self.worker_id, self.settings.worker_poll_s
        )
        while not stop.is_set():
            try:
                self.requeue_stale()
                claimed = self.run_once()
            except Exception:  # pragma: no cover - the loop must outlive one bad job
                log.exception("worker %s: unhandled error while running a job", self.worker_id)
                claimed = None
            if claimed is None:
                stop.wait(self.settings.worker_poll_s)

    def requeue_stale(self) -> list[str]:
        """Return jobs whose worker stopped reporting to the queue.

        A worker killed by an OOM, a deploy or a `docker stop` leaves a row saying `running`
        forever. Requeued rather than failed, because the build's own cache means the next attempt
        resumes -- and `--out` is not touched, which is what makes that true.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.job_heartbeat_timeout_s)
        requeued = []
        for job in self.index.stale_running_jobs(before=cutoff):
            if job.attempts >= self.settings.job_max_attempts:
                self.index.finish_job(
                    job_id=job.id,
                    state="failed",
                    at=datetime.now(UTC),
                    error=(
                        f"the worker running this job stopped reporting, and it has already been "
                        f"attempted {job.attempts} time(s) (limit "
                        f"{self.settings.job_max_attempts}). Completed stages are still cached "
                        f"under the trip's output directory, so a new job resumes rather than "
                        f"restarts."
                    ),
                )
                continue
            self.index.requeue_job(
                job_id=job.id,
                at=datetime.now(UTC),
                error=(
                    f"worker {job.worker_id} stopped reporting (no heartbeat since "
                    f"{job.heartbeat_at}); requeued to resume."
                ),
            )
            requeued.append(job.id)
        return requeued

    def run_once(self) -> JobOutcome | None:
        """Claim one job and run it to completion. None when the queue is empty."""
        job = self.index.claim_next_job(worker_id=self.worker_id, now=datetime.now(UTC))
        if job is None:
            return None
        log.info("worker %s claimed job %s (trip %s)", self.worker_id, job.id, job.trip_id)
        try:
            return self._execute(job)
        except Exception as exc:
            log.exception("job %s failed", job.id)
            return self._fail(job, f"{type(exc).__name__}: {exc}")

    # --- one job ----------------------------------------------------------------------------

    def _fail(self, job: Job, error: str, exit_code: int | None = None) -> JobOutcome:
        self.index.finish_job(
            job_id=job.id,
            state="failed",
            at=datetime.now(UTC),
            error=error,
            exit_code=exit_code,
        )
        return JobOutcome(job_id=job.id, state="failed", error=error, exit_code=exit_code)

    def _execute(self, job: Job) -> JobOutcome:
        paths = trip_paths(self.settings, job.trip_id)
        now = datetime.now(UTC)
        self.index.heartbeat_job(job_id=job.id, phase="prepare", at=now)

        prepared = self._prepare(job, paths)
        if prepared is not None:
            return prepared

        if job.kind == "reel":
            self.index.heartbeat_job(job_id=job.id, phase="render", at=datetime.now(UTC))
            return self._reel(job, paths)

        # Measured before the build starts, so a client polling a running job already knows which
        # stages this deployment cannot run and why.
        self.index.record_job_capability(
            job_id=job.id,
            capability=json.dumps(
                measure_capability(
                    out_dir=paths.out,
                    source_dir=paths.source,
                    config_path=paths.config,
                    no_cloud=self.settings.build_no_cloud,
                )
            ),
        )

        self.index.heartbeat_job(job_id=job.id, phase="build", at=datetime.now(UTC))
        return self._build(job, paths)

    def _prepare(self, job: Job, paths) -> JobOutcome | None:
        """Fetch the media and scaffold the config. Returns an outcome only on failure.

        `source:prepare` is a route of its own (S02) and this is the same two functions, called as
        step zero: a client should not have to make two calls in order, and the operation is
        idempotent, so calling both costs nothing.
        """
        if self.store is None:
            return self._fail(
                job,
                "no object store is configured, so the uploaded media cannot be fetched. Set "
                "STORY_SERVICE_S3_BUCKET (and STORY_SERVICE_S3_ENDPOINT_URL for a local MinIO or "
                "moto).",
            )
        assets = self.index.trip_assets(trip_id=job.trip_id)
        if not assets:
            return self._fail(
                job,
                "this trip has declared no assets, so there is nothing to work from. Negotiate "
                "and upload first; a build over an empty folder exits 0 and produces a trip with "
                "no days in it, which is worse than this error.",
            )
        try:
            materialised = materialise_source(
                settings=self.settings,
                store=self.store,
                owner_id=job.owner_id,
                trip_id=job.trip_id,
                assets=assets,
            )
        except ObjectStoreError as exc:
            return self._fail(job, f"could not fetch the uploaded media: {exc}")

        if not materialised.complete:
            return self._fail(
                job,
                f"{len(materialised.missing)} of {len(assets)} declared asset(s) have not been "
                f"uploaded: {', '.join(m[:12] for m in materialised.missing)}. Building now would "
                "produce a trip missing an afternoon and report success.",
            )
        try:
            scaffold_trip(
                settings=self.settings, trip_id=job.trip_id, trip_name=self._trip_name(job)
            )
        except ScaffoldError as exc:
            return self._fail(job, f"could not scaffold the trip config: {exc}")
        return None

    def _trip_name(self, job: Job) -> str:
        trip = self.index.get_trip(owner_id=job.owner_id, trip_id=job.trip_id)
        return trip.name if trip is not None else job.trip_id

    def _build_argv(self, paths) -> list[str]:
        argv = [
            self.settings.story_book_bin,
            "build",
            str(paths.source),
            "--out",
            str(paths.out),
        ]
        if paths.config.exists():
            argv += ["--config", str(paths.config)]
        if paths.overrides.exists():
            # The corrections file is the trip's own. I43 will write it through a route that does
            # not exist yet; until then it is the empty file `init` scaffolded, and passing it keeps
            # one code path for both.
            argv += ["--overrides", str(paths.overrides)]
        if self.settings.build_no_cloud:
            argv.append("--no-cloud")
        return argv

    def _run_cli(self, job: Job, paths, argv: list[str], *, phase: str) -> tuple[int, Path]:
        """Run one CLI invocation, log its output verbatim, and return `(exit_code, log_path)`.

        Shared by `_build` and `_reel`: both run a subprocess of this same binary, both must keep
        its own output rather than a paraphrase of it, and both need the heartbeat kept alive for
        as long as the process runs -- at the phase already current for this job, so a build's
        own heartbeat cannot be mistaken for `render` or vice versa.
        """
        log_path = paths.trip_dir / "logs" / f"{job.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as sink:
            sink.write(f"\n$ {' '.join(argv)}\n".encode())
            sink.flush()
            process = subprocess.Popen(argv, stdout=sink, stderr=subprocess.STDOUT)
            returncode = self._wait_with_heartbeat(job, process, phase=phase)
        return returncode, log_path

    def _build(self, job: Job, paths) -> JobOutcome:
        argv = self._build_argv(paths)
        returncode, log_path = self._run_cli(job, paths, argv, phase="build")

        if returncode == INTERRUPTED_EXIT_CODE:
            if job.attempts >= self.settings.job_max_attempts:
                return self._fail(
                    job,
                    f"the build was interrupted on attempt {job.attempts} of "
                    f"{self.settings.job_max_attempts}. Everything it finished is still cached, so "
                    "a new job resumes from there.",
                    exit_code=returncode,
                )
            self.index.requeue_job(
                job_id=job.id,
                at=datetime.now(UTC),
                error=(
                    f"the build exited {returncode} (interrupted) on attempt {job.attempts}; "
                    "requeued. Finished items are committed, so the next attempt resumes."
                ),
            )
            return JobOutcome(job_id=job.id, state="queued", exit_code=returncode)

        tail = _tail(log_path)
        if returncode == FAILED_ITEMS_EXIT_CODE:
            return self._fail(
                job,
                "story-book build exited 1: it reached the end of the pipeline and some items "
                f"failed. A partial trip.json may exist. The build's own last words: {tail}",
                exit_code=returncode,
            )
        if returncode != 0:
            return self._fail(
                job,
                f"story-book build exited {returncode}. The build's own last words: {tail}",
                exit_code=returncode,
            )
        if not (paths.out / "trip.json").is_file():
            # Exit 0 is not the artifact. P06's nine JPEGs under `.mov` names are the standing
            # reminder that a successful-looking process is not a check on what it produced.
            return self._fail(
                job,
                f"story-book build exited 0 but wrote no {paths.out / 'trip.json'}. trip.json is "
                "the canonical artifact; without it there is nothing for the client to read.",
                exit_code=returncode,
            )
        self.index.finish_job(
            job_id=job.id, state="succeeded", at=datetime.now(UTC), exit_code=returncode
        )
        return JobOutcome(job_id=job.id, state="succeeded", exit_code=returncode)

    def _reel(self, job: Job, paths) -> JobOutcome:
        """Run `story-book reel` for a `kind == "reel"` job.

        A reel renders from `trip.json` and never touches `story.db` (`export/reel.py`'s own
        docstring), so unlike `_build` there is no pipeline stage progress to read live -- instead
        `reel_progress_seed` computes the exact segment plan once, up front, and progress is a
        real count of that plan's own cache files as they appear on disk (`reel_jobs.py`).
        """
        trip_json_path = paths.out / "trip.json"
        if not trip_json_path.is_file():
            return self._fail(
                job,
                f"no {trip_json_path}: this trip has not been built yet. A reel renders from "
                "trip.json, so a successful build has to exist first.",
            )
        try:
            options: dict = json.loads(job.options) if job.options else {}
        except json.JSONDecodeError:
            return self._fail(
                job, "internal error: this reel job's own options could not be read back"
            )

        music_path = None
        music_hash = options.get("music_hash")
        if music_hash:
            assets = {a.media_hash: a for a in self.index.trip_assets(trip_id=job.trip_id)}
            asset = assets.get(music_hash)
            if asset is None:
                return self._fail(job, f"music asset {music_hash} is not declared on this trip")
            candidate = paths.source / asset.stored_filename
            if not candidate.is_file():
                return self._fail(
                    job,
                    f"music asset {music_hash} ({asset.filename}) is declared but has not been "
                    "uploaded; negotiate and PUT it before requesting a reel with music",
                )
            music_path = candidate

        seed = reel_progress_seed(paths=paths, options=options)
        if seed is not None:
            self.index.record_job_progress(job_id=job.id, progress=json.dumps(seed))

        argv = reel_argv(self.settings.story_book_bin, paths, options, music_path)
        returncode, log_path = self._run_cli(job, paths, argv, phase="render")
        tail = _tail(log_path)

        if returncode != 0:
            # Unlike `story-book build`, the reel CLI has no documented "interrupted, resume"
            # exit code of its own (its render loop is not the pipeline `Runner`), so there is no
            # honest way to distinguish "killed" from "genuinely failed" here -- inventing one
            # would be exactly the fabricated distinction this project's rules warn against.
            # Rendering is still resumable at the segment-cache level: a fresh reel request for
            # the same options reuses whatever `.cache/segments/` already holds.
            return self._fail(
                job,
                f"story-book reel exited {returncode}. The render's own last words: {tail}",
                exit_code=returncode,
            )

        video_path, manifest_path = reel_output_paths(paths, options)
        if not video_path.is_file() or not manifest_path.is_file():
            # Exit 0 is not the artifact -- the same P06 lesson `_build` already applies.
            missing = video_path if not video_path.is_file() else manifest_path
            return self._fail(
                job,
                f"story-book reel exited 0 but wrote no {missing}. trip.mp4 and reel.json "
                "together are the artifact; without both there is nothing for the client to read.",
                exit_code=returncode,
            )
        self.index.finish_job(
            job_id=job.id, state="succeeded", at=datetime.now(UTC), exit_code=returncode
        )
        return JobOutcome(job_id=job.id, state="succeeded", exit_code=returncode)

    def _wait_with_heartbeat(self, job: Job, process: subprocess.Popen, *, phase: str) -> int:
        while True:
            try:
                return process.wait(timeout=self.settings.job_heartbeat_s)
            except subprocess.TimeoutExpired:
                self.index.heartbeat_job(job_id=job.id, phase=phase, at=datetime.now(UTC))


def _tail(path: Path, limit: int = 600) -> str:
    try:
        text = path.read_text(errors="replace").strip()
    except OSError as exc:  # pragma: no cover - the file was just written
        return f"(the build log could not be read: {exc})"
    return text[-limit:] if text else "(the build wrote nothing)"


def start_inline_worker(app_state) -> None:
    """Run a worker in a thread of the API process, on its **own** index connection.

    Its own connection, not the API's: that is how a separate worker process would run, so the
    inline case exercises the same cross-connection behaviour -- and it is why WAL is a requirement
    rather than a tuning choice.
    """
    from storybook_service import index as index_module

    settings: Settings = app_state.settings
    index = index_module.for_dsn(settings.resolved_index_dsn())
    store = getattr(app_state, "object_store", None)
    worker = Worker(settings, index, store)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,), name="storybook-worker")
    thread.daemon = True
    thread.start()
    app_state.worker = worker
    app_state.worker_stop = stop
    app_state.worker_thread = thread
    app_state.worker_index = index


def stop_inline_worker(app_state) -> None:
    stop = getattr(app_state, "worker_stop", None)
    if stop is None:
        return
    stop.set()
    thread = getattr(app_state, "worker_thread", None)
    if thread is not None:
        thread.join(timeout=30)
    index = getattr(app_state, "worker_index", None)
    if index is not None:
        index.close()


def main() -> None:  # pragma: no cover - the separate-process entry point
    """`python -m storybook_service.worker`, for running the worker beside the API.

    Same image, same code, same claim transaction. Set STORY_SERVICE_WORKER_INLINE=0 on the API
    container when you do this, or two workers will poll one queue -- which is safe, and pointless
    on one machine.
    """
    logging.basicConfig(level=logging.INFO)
    from storybook_service import index as index_module
    from storybook_service.objectstore import S3ObjectStore

    settings = Settings.from_env()
    index = index_module.for_dsn(settings.resolved_index_dsn())
    try:
        store = S3ObjectStore(settings)
    except ObjectStoreError as exc:
        log.warning("no object store configured (%s); jobs will fail at the fetch step", exc)
        store = None
    stop = threading.Event()
    try:
        Worker(settings, index, store).run_forever(stop)
    except KeyboardInterrupt:
        log.info("worker stopping; a claimed job is requeued once its heartbeat goes stale")
    finally:
        index.close()


if __name__ == "__main__":  # pragma: no cover
    main()
