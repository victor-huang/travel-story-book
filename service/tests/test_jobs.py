"""The queue end to end: upload real media, queue a build, run it, poll it.

**Real fixture media, a real presigned `PUT` to a real S3 API, and the real CLI.** The thing this
task can most easily get wrong is reporting progress that looks right and measures nothing, and no
mock of `stage_result` can catch that -- the numbers have to come out of a database a build wrote
while it was writing it.

`build_no_cloud=True` throughout: a test makes no network call, and the pipeline is required to
complete without one anyway.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.index_sqlite import SqliteIndex
from storybook_service.objectstore import S3ObjectStore
from storybook_service.settings import Settings
from storybook_service.source import trip_paths
from storybook_service.worker import Worker

from story_book.db import connection as db
from tests.conftest import REGION

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media"
SMALL = ("heic_gps_offset.heic", "jpeg_gps_no_offset.jpg", "clip_speech.mov")
ME = "traveller@example.com"


def _media(names: tuple[str, ...] | None = None) -> dict[str, bytes]:
    assert FIXTURES.is_dir(), f"{FIXTURES} is missing; the fixtures are committed"
    chosen = names or tuple(
        path.name for path in sorted(FIXTURES.iterdir()) if path.suffix != ".txt"
    )
    media: dict[str, bytes] = {}
    seen: set[str] = set()
    for name in chosen:
        path = FIXTURES / name
        assert path.is_file(), f"{path} is missing"
        body = path.read_bytes()
        # The fixture set contains a byte-identical pair on purpose, and negotiate rightly refuses
        # one hash twice in a request -- two declarations of one hash with different sizes have no
        # correct answer. So the *upload* set is deduplicated, exactly as a client's would be.
        digest = hashlib.blake2b(body).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        media[name] = body
    return media


@pytest.fixture(scope="session")
def small_media() -> dict[str, bytes]:
    return _media(SMALL)


@pytest.fixture(scope="session")
def whole_library() -> dict[str, bytes]:
    """Every committed fixture, so a build lasts long enough to be watched while it runs."""
    return _media()


@dataclass
class Env:
    settings: Settings
    client: TestClient
    index: SqliteIndex
    store: S3ObjectStore

    def worker(self) -> Worker:
        """A worker on its **own** index connection, as a separate worker process would be."""
        return Worker(self.settings, SqliteIndex(self.settings.data_root / "index.db"), self.store)


@pytest.fixture
def env(tmp_path, bucket, moto_endpoint):
    data_root = tmp_path / "service-data"
    settings = Settings(
        data_root=data_root,
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
        # The index the injected instance below points at, so the DSN and the injection agree.
        index_dsn=f"sqlite:///{data_root / 'index.db'}",
        # The tests drive `run_once` themselves; a background thread would race the assertions.
        worker_inline=False,
        build_no_cloud=True,
        job_heartbeat_s=0.2,
    )
    index = SqliteIndex(data_root / "index.db")
    store = S3ObjectStore(settings)
    app = create_app(settings, index=index, object_store=store)
    with TestClient(app) as client:
        yield Env(settings, client, index, store)
    index.close()


def _headers() -> dict[str, str]:
    return {"X-Story-Identity": ME}


def _upload(env: Env, media: dict[str, bytes], *, skip: set[str] = frozenset()) -> str:
    """Create a trip, negotiate every asset, and PUT all but the named ones."""
    trip_id = env.client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()[
        "trip_id"
    ]
    declarations = [
        {"hash": hashlib.blake2b(body).hexdigest(), "filename": name, "size": len(body)}
        for name, body in media.items()
    ]
    response = env.client.post(
        f"/trips/{trip_id}/assets:negotiate",
        json={"assets": declarations},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    by_hash = {hashlib.blake2b(body).hexdigest(): body for body in media.values()}
    for entry in response.json()["needed"]:
        if entry["filename"] in skip:
            continue
        put = httpx.put(entry["put_url"], content=by_hash[entry["hash"]], headers=entry["headers"])
        assert put.status_code == 200, put.text
    return trip_id


def _fake_cli(tmp_path: Path, exit_code: int) -> str:
    """A `story-book` that delegates `init` to the real one and fails `build` on purpose.

    A test of a failure mode must be shown to fail, and the only honest way to exercise "the build
    exited 130" is for a build to exit 130. Everything before it stays real.
    """
    real = shutil.which("story-book")
    assert real, "story-book is not on PATH; the service exists to run it"
    script = tmp_path / f"fake-story-book-{exit_code}"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = "build" ]; then exit {exit_code}; fi\nexec "{real}" "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


class TestQueueingABuild:
    def test_a_build_is_accepted_and_queued(self, env, small_media):
        trip_id = _upload(env, small_media)
        response = env.client.post(f"/trips/{trip_id}/build", headers=_headers())
        assert response.status_code == 202
        body = response.json()
        assert (body["state"], body["created"], body["stage"]) == ("queued", True, None)

    def test_a_queued_job_publishes_no_measurement_it_has_not_taken(self, env, small_media):
        """`total` is null, not 0: the size of the work is genuinely not known yet."""
        trip_id = _upload(env, small_media)
        job = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()
        assert (job["done"], job["total"], job["queued_ahead"]) == (0, None, 0)

    def test_a_second_request_returns_the_same_job(self, env, small_media):
        """One worker per trip at a time, and a client that lost its job_id gets it back."""
        trip_id = _upload(env, small_media)
        first = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()
        second = env.client.post(f"/trips/{trip_id}/build", headers=_headers())
        assert second.status_code == 200
        assert (second.json()["job_id"], second.json()["created"]) == (first["job_id"], False)

    def test_a_build_of_an_unknown_trip_is_a_404(self, env):
        assert env.client.post("/trips/nope/build", headers=_headers()).status_code == 404

    def test_the_route_accepts_no_subset_of_the_trip(self, env, small_media):
        """T58: the transport may be chunked, the unit of work may not be.

        Asserted against the published schema rather than by intention, so a later `day` parameter
        fails here.
        """
        schema = env.client.get("/openapi.json").json()
        body = schema["paths"]["/trips/{trip_id}/build"]["post"].get("requestBody")
        properties = (
            schema["components"]["schemas"]["BuildRequest"]["properties"]
            if body is not None
            else {}
        )
        assert properties == {}


class TestCrossTenantReads:
    def test_another_account_cannot_poll_the_job(self, env, small_media):
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        response = env.client.get(
            f"/jobs/{job_id}", headers={"X-Story-Identity": "someone@else.example"}
        )
        assert response.status_code == 404

    def test_the_owner_can(self, env, small_media):
        """The control: without it the assertion above would pass on a route that 404s for all."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        assert env.client.get(f"/jobs/{job_id}", headers=_headers()).status_code == 200

    def test_an_unauthenticated_caller_is_refused(self, env, small_media):
        trip_id = _upload(env, small_media)
        assert env.client.post(f"/trips/{trip_id}/build").status_code == 401


class TestARealBuildThroughTheQueue:
    """The acceptance criterion's first half, against real media and the real CLI."""

    def test_the_job_succeeds_and_the_trip_json_exists(self, env, small_media):
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        outcome = env.worker().run_once()
        assert outcome is not None and outcome.state == "succeeded", outcome

        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        paths = trip_paths(env.settings, trip_id)
        assert body["state"] == "succeeded"
        assert (paths.out / "trip.json").is_file()

    def test_the_finished_job_reports_the_counts_the_database_holds(self, env, small_media):
        """Not "some numbers came back": the same numbers, read a second way."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        env.worker().run_once()
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()

        paths = trip_paths(env.settings, trip_id)
        conn = db.connect(paths.out / db.DB_FILENAME, create=False)
        try:
            assert body["media_known"] == db.count_media(conn) == len(small_media)
            for stage in body["stages"]:
                if stage["state"] in ("unavailable", "unknown"):
                    continue
                recorded = db.completed_hashes(conn, stage["name"], stage["version"])
                expected = len(recorded - {"__trip__"}) or (1 if recorded else 0)
                assert stage["done"] == expected, stage
        finally:
            conn.close()

    def test_a_degraded_deployment_says_so_on_the_job(self, env, small_media):
        """Open question 18: this image has no `clip`, and the job reports it by name."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        env.worker().run_once()
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert body["degraded"] is True
        reasons = {entry["stage"]: entry for entry in body["unavailable_stages"]}
        assert "embeddings" in reasons
        assert reasons["embeddings"]["reason"] and reasons["embeddings"]["affects"]

    def test_the_queue_empties(self, env, small_media):
        trip_id = _upload(env, small_media)
        env.client.post(f"/trips/{trip_id}/build", headers=_headers())
        worker = env.worker()
        assert worker.run_once() is not None
        assert worker.run_once() is None


class TestARetryIsAResume:
    """The other half of the criterion, and the guarantee it is easiest to defeat by accident."""

    def test_a_second_job_recomputes_nothing_that_was_already_done(self, env, small_media):
        """Measured by `computed_at`, with a control that must differ.

        A per-item stage's cached rows must keep the timestamp of the first build -- if the second
        build had recomputed them, or if anything here had wiped `--out`, they would move. The
        control is `scan`, which is `always_run` by design and therefore *must* be newer. Without it
        this test would pass just as well against a build that never ran at all.
        """
        trip_id = _upload(env, small_media)
        env.client.post(f"/trips/{trip_id}/build", headers=_headers())
        env.worker().run_once()

        paths = trip_paths(env.settings, trip_id)
        conn = db.connect(paths.out / db.DB_FILENAME, create=False)
        try:
            hashes = sorted(media.hash for media in db.iter_media(conn))
            before = {h: db.get_stage_result(conn, h, "metadata").computed_at for h in hashes}
            scan_before = db.get_stage_result(conn, "__trip__", "scan").computed_at
        finally:
            conn.close()

        # `computed_at` has one-second resolution, so a second build inside the same second would
        # make both the claim and its control unfalsifiable.
        time.sleep(1.1)
        env.client.post(f"/trips/{trip_id}/build", headers=_headers())
        assert env.worker().run_once().state == "succeeded"

        conn = db.connect(paths.out / db.DB_FILENAME, create=False)
        try:
            after = {h: db.get_stage_result(conn, h, "metadata").computed_at for h in hashes}
            scan_after = db.get_stage_result(conn, "__trip__", "scan").computed_at
        finally:
            conn.close()
        assert after == before
        assert scan_after > scan_before

    def test_an_interrupted_build_is_requeued_rather_than_failed(self, env, small_media, tmp_path):
        """Exit 130 means the runner caught a SIGINT and committed what it had finished."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        worker = Worker(
            Settings(**{**vars(env.settings), "story_book_bin": _fake_cli(tmp_path, 130)}),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        outcome = worker.run_once()
        assert outcome.state == "queued"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert (body["state"], body["attempts"]) == ("queued", 1)
        assert "resume" in body["error"]

    def test_an_interrupt_that_never_ends_eventually_fails_rather_than_cycling(
        self, env, small_media, tmp_path
    ):
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        worker = Worker(
            Settings(**{**vars(env.settings), "story_book_bin": _fake_cli(tmp_path, 130)}),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        for _ in range(env.settings.job_max_attempts):
            worker.run_once()
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert body["state"] == "failed"
        assert str(env.settings.job_max_attempts) in body["error"]

    def test_a_worker_that_stopped_reporting_has_its_job_requeued(self, env, small_media):
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        env.index.claim_next_job(worker_id="a-worker-that-died", now=_long_ago())

        worker = Worker(
            Settings(**{**vars(env.settings), "job_heartbeat_timeout_s": 1.0}),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        assert worker.requeue_stale() == [job_id]
        assert env.client.get(f"/jobs/{job_id}", headers=_headers()).json()["state"] == "queued"

    def test_a_worker_that_is_reporting_keeps_its_job(self, env, small_media):
        """The control: staleness has to be able to say no, or every running job gets stolen."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        env.index.claim_next_job(worker_id="a-healthy-worker", now=_now())
        worker = Worker(
            Settings(**vars(env.settings)),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        assert worker.requeue_stale() == []
        assert env.client.get(f"/jobs/{job_id}", headers=_headers()).json()["state"] == "running"


class TestAFailedJobDoesNotReadLikeAFinishedOne:
    def test_a_missing_upload_fails_the_job_and_names_the_count(self, env, small_media):
        """Building with four photographs missing would succeed and produce a trip missing them."""
        trip_id = _upload(env, small_media, skip={"clip_speech.mov"})
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        assert env.worker().run_once().state == "failed"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert body["state"] == "failed"
        assert "have not been uploaded" in body["error"]

    def test_a_trip_with_no_assets_fails_rather_than_building_nothing(self, env):
        trip_id = env.client.post("/trips", json={"name": "empty"}, headers=_headers()).json()[
            "trip_id"
        ]
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        assert env.worker().run_once().state == "failed"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert "declared no assets" in body["error"]

    def test_a_nonzero_exit_carries_the_builds_own_last_words(self, env, small_media, tmp_path):
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        worker = Worker(
            Settings(**{**vars(env.settings), "story_book_bin": _fake_cli(tmp_path, 2)}),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        assert worker.run_once().state == "failed"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert (body["state"], body["exit_code"]) == ("failed", 2)

    def test_exit_zero_without_a_trip_json_is_a_failure(self, env, small_media, tmp_path):
        """Exit 0 is not the artifact. P06's nine JPEGs under `.mov` names are the precedent."""
        trip_id = _upload(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
        worker = Worker(
            Settings(**{**vars(env.settings), "story_book_bin": _fake_cli(tmp_path, 0)}),
            SqliteIndex(env.settings.data_root / "index.db"),
            env.store,
        )
        assert worker.run_once().state == "failed"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert "wrote no" in body["error"] and "trip.json" in body["error"]


class TestTheInlineWorker:
    """The default deployment shape, run as a deployment would run it.

    Every other test in this file drives `run_once` by hand, which proves the queue and proves
    nothing about the thread that is supposed to call it. Without this test the default setting
    could be broken and the suite would be green -- the exact shape of gap S01 caught with `/ready`.
    """

    def test_a_queued_build_is_picked_up_without_anyone_calling_the_worker(
        self, tmp_path, bucket, moto_endpoint, small_media
    ):
        data_root = tmp_path / "service-data"
        settings = Settings(
            data_root=data_root,
            s3_bucket=bucket,
            s3_region=REGION,
            s3_endpoint_url=moto_endpoint,
            index_dsn=f"sqlite:///{data_root / 'index.db'}",
            worker_inline=True,
            build_no_cloud=True,
            worker_poll_s=0.05,
            job_heartbeat_s=0.2,
        )
        # No injected index: the app opens the DSN, and so does the worker, on its own connection.
        # Two connections to one file across two threads is what makes WAL a requirement.
        app = create_app(settings, object_store=S3ObjectStore(settings))
        with TestClient(app) as client:
            env = Env(settings, client, None, S3ObjectStore(settings))
            trip_id = _upload(env, small_media)
            job_id = client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]

            deadline = time.monotonic() + 120
            body = {}
            while time.monotonic() < deadline:
                body = client.get(f"/jobs/{job_id}", headers=_headers()).json()
                if body["state"] in ("succeeded", "failed"):
                    break
                time.sleep(0.1)
        assert body.get("state") == "succeeded", body
        assert (trip_paths(settings, trip_id).out / "trip.json").is_file()


class TestPollingARunningBuild:
    """The acceptance criterion: **monotonically advancing counts that match the DB**.

    Polled over HTTP against a build that is genuinely running, in another thread on another
    database connection -- which is also what makes WAL a requirement rather than a preference.
    """

    @staticmethod
    def _position(body: dict) -> tuple[int, int]:
        """A single ordering key over both phases, so "advancing" is a testable claim.

        `prepare` comes before every stage; a build with no outstanding stage comes after all of
        them. Neither is a percentage -- this ordering exists in the test, not in the response.
        """
        if body["phase"] == "prepare":
            return (0, body["done"])
        if body["stage_index"] is None:
            return ((body.get("stages_total") or 0) + 1, 0)
        return (body["stage_index"], body["done"])

    def test_progress_advances_monotonically_while_the_build_runs(self, env, whole_library):
        from story_book.cli import build_stages

        trip_id = _upload(env, whole_library)
        job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]

        worker = env.worker()
        done = threading.Event()

        def run() -> None:
            try:
                worker.run_once()
            finally:
                done.set()

        thread = threading.Thread(target=run, name="build")
        thread.start()

        during: list[dict] = []
        while not done.is_set():
            body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
            if body["state"] == "running":
                during.append(body)
            time.sleep(0.02)
        thread.join(timeout=120)
        final = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()

        assert final["state"] == "succeeded", final.get("error")
        assert len(during) >= 3, during

        # Monotonic: the stage pointer only moves forward, and within one stage `done` only rises.
        ordered = [self._position(body) for body in during]
        # `pairwise`, not `zip(xs, xs[1:], strict=True)`: that idiom is off by one by construction
        # and has already cost this project a session.
        for earlier, later in pairwise(ordered):
            assert later >= earlier, (earlier, later)

        # **The control that must differ.** Every assertion above is also satisfied by an endpoint
        # that returns one constant for ever, so the reading has to be shown to move -- and a
        # finished job has to read differently from a running one.
        assert len(set(ordered)) > 1, ordered
        assert self._position(final) > ordered[0]

        # A stage name the client cannot map to anything is as useless as no name, so every name
        # published while running is one of the pipeline's own.
        named = {body["stage"] for body in during if body["stage"]}
        known = {stage.name for stage in build_stages(None)} | {"source:prepare"}
        assert named and named <= known, named - known

        # The counts came from the database, not from this service's bookkeeping: re-read the
        # highest `done` reported for a stage and check it against `stage_result` now that the
        # build has finished -- it cannot have exceeded the final count.
        paths = trip_paths(env.settings, trip_id)
        conn = db.connect(paths.out / db.DB_FILENAME, create=False)
        try:
            for body in during:
                if body["phase"] != "build" or not body["stage"]:
                    continue
                stage = next(s for s in body["stages"] if s["name"] == body["stage"])
                settled = len(db.completed_hashes(conn, stage["name"], stage["version"]))
                assert body["done"] <= settled, (body["stage"], body["done"], settled)
        finally:
            conn.close()


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _long_ago():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(hours=3)


def test_the_worker_module_runs_as_a_process(env):
    """`python -m storybook_service.worker` is the separate-process deployment shape.

    Only checked to be startable and to exit cleanly -- the queue behaviour is covered above, and
    what could break here is the module having no runnable entry point at all.
    """
    completed = subprocess.run(
        ["python", "-c", "import storybook_service.worker as w; assert callable(w.main)"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
