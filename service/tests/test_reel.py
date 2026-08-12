"""S07 end to end: real fixture media, a real build, a real reel render through the queue.

Same discipline as `test_jobs.py` and `test_delivery.py`: the thing easiest to get wrong here is
progress that looks real and measures nothing, and a declared video that is not actually a video
(P06) -- so this file reads bytes rather than trusting a 200, and checks a control (silent vs.
with-music) rather than a single reading.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.index_sqlite import SqliteIndex
from storybook_service.objectstore import LocalFileObjectStore, S3ObjectStore
from storybook_service.settings import Settings
from storybook_service.source import trip_paths
from storybook_service.worker import Worker

from tests.conftest import REGION

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media"
SMALL = ("heic_gps_offset.heic", "jpeg_gps_no_offset.jpg", "clip_speech.mov")
ME = "traveller@example.com"


def _media(names: tuple[str, ...]) -> dict[str, bytes]:
    assert FIXTURES.is_dir(), f"{FIXTURES} is missing; the fixtures are committed"
    media: dict[str, bytes] = {}
    for name in names:
        path = FIXTURES / name
        assert path.is_file(), f"{path} is missing"
        media[name] = path.read_bytes()
    return media


@pytest.fixture(scope="session")
def small_media() -> dict[str, bytes]:
    return _media(SMALL)


@pytest.fixture(scope="session")
def music_bytes(tmp_path_factory) -> bytes:
    """A tiny, real, ffmpeg-decodable audio file -- the same generator `tests/backend/test_reel.py`
    uses for `--music`, so this is a real track, not bytes shaped like one."""
    target = tmp_path_factory.mktemp("music") / "track.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "aac",
            str(target),
        ],  # fmt: skip
        check=True,
    )
    return target.read_bytes()


@dataclass
class Env:
    settings: Settings
    client: TestClient
    index: SqliteIndex
    store: object

    def worker(self) -> Worker:
        return Worker(self.settings, SqliteIndex(self.settings.data_root / "index.db"), self.store)


@pytest.fixture
def env(tmp_path, bucket, moto_endpoint):
    data_root = tmp_path / "service-data"
    settings = Settings(
        data_root=data_root,
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
        index_dsn=f"sqlite:///{data_root / 'index.db'}",
        worker_inline=False,
        build_no_cloud=True,
        job_heartbeat_s=0.2,
        delivery_presign_ttl_s=120,
    )
    index = SqliteIndex(data_root / "index.db")
    store = S3ObjectStore(settings)
    app = create_app(settings, index=index, object_store=store)
    with TestClient(app) as client:
        yield Env(settings, client, index, store)
    index.close()


def _headers(identity: str = ME) -> dict[str, str]:
    return {"X-Story-Identity": identity}


def _put(env: Env, url: str, content: bytes, headers: dict[str, str]):
    """PUT to a presigned URL -- in-process for the local backend, real HTTP for S3/moto.

    `LocalFileObjectStore`'s "presigned" URL is an ordinary route on *this* app (app.py), not a
    real server anywhere -- issuing a genuine `httpx.put` against it would depend on whatever
    happens to be listening on `settings.public_base_url` on this machine, which on this exact
    project's dev box is sometimes a real service (see the tracker's own log of finding and
    killing an unrelated process on a guessed-free port). Routing through the same `TestClient`
    the app itself is being tested with is what makes this deterministic.
    """
    base = env.settings.public_base_url
    if env.settings.object_store_backend == "local" and base and url.startswith(base):
        return env.client.put(url[len(base) :], content=content, headers=headers)
    return httpx.put(url, content=content, headers=headers)


def _declare_and_upload(env: Env, trip_id: str, media: dict[str, bytes]) -> dict[str, str]:
    """Negotiate and PUT every asset; return `{filename: hash}`."""
    declarations = [
        {"hash": hashlib.blake2b(body).hexdigest(), "filename": name, "size": len(body)}
        for name, body in media.items()
    ]
    negotiated = env.client.post(
        f"/trips/{trip_id}/assets:negotiate", json={"assets": declarations}, headers=_headers()
    )
    assert negotiated.status_code == 200, negotiated.text
    by_hash = {hashlib.blake2b(body).hexdigest(): body for body in media.values()}
    for entry in negotiated.json()["needed"]:
        put = _put(env, entry["put_url"], by_hash[entry["hash"]], entry["headers"])
        assert put.status_code == 200, put.text
    return {name: hashlib.blake2b(body).hexdigest() for name, body in media.items()}


def _built_trip(env: Env, media: dict[str, bytes]) -> tuple[str, dict[str, str]]:
    """A trip with a *succeeded* build -- the precondition every reel job needs."""
    trip_id = env.client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()[
        "trip_id"
    ]
    hashes = _declare_and_upload(env, trip_id, media)
    build_job = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
    outcome = env.worker().run_once()
    assert outcome is not None and outcome.state == "succeeded", outcome
    assert env.client.get(f"/jobs/{build_job}", headers=_headers()).json()["state"] == "succeeded"
    return trip_id, hashes


class TestQueueingAReel:
    def test_a_reel_is_accepted_and_queued(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        response = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers())
        assert response.status_code == 202
        body = response.json()
        assert (body["kind"], body["state"], body["created"]) == ("reel", "queued", True)

    def test_a_reel_before_any_build_is_still_queueable(self, env):
        """Queueing is cheap; the worker is what refuses a trip with no trip.json yet."""
        trip_id = env.client.post("/trips", json={"name": "empty"}, headers=_headers()).json()[
            "trip_id"
        ]
        response = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers())
        assert response.status_code == 202

    def test_a_reel_of_an_unknown_trip_is_a_404(self, env):
        assert env.client.post("/trips/nope/reel", json={}, headers=_headers()).status_code == 404

    def test_a_second_reel_request_returns_the_same_queued_job(self, env, small_media):
        """One active job per trip regardless of kind: both write under the same --out."""
        trip_id, _hashes = _built_trip(env, small_media)
        first = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers()).json()
        second = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers())
        assert second.status_code == 200
        assert (second.json()["job_id"], second.json()["created"]) == (first["job_id"], False)

    def test_a_bad_aspect_is_refused_before_a_job_is_queued(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        response = env.client.post(
            f"/trips/{trip_id}/reel", json={"aspect": "nonsense"}, headers=_headers()
        )
        assert response.status_code == 422

    def test_an_undeclared_music_hash_is_refused_before_a_job_is_queued(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        response = env.client.post(
            f"/trips/{trip_id}/reel",
            json={"music_hash": "a" * 128},
            headers=_headers(),
        )
        assert response.status_code == 422
        assert "not a declared asset" in response.json()["detail"]

    def test_an_unauthenticated_caller_is_refused(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        assert env.client.post(f"/trips/{trip_id}/reel", json={}).status_code == 401


class TestCrossTenantReads:
    def test_another_account_cannot_poll_or_download_the_reel(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers()).json()[
            "job_id"
        ]
        other = {"X-Story-Identity": "someone@else.example"}
        assert env.client.get(f"/jobs/{job_id}", headers=other).status_code == 404
        assert env.client.get(f"/jobs/{job_id}/reel", headers=other).status_code == 404
        assert env.client.get(f"/jobs/{job_id}", headers=_headers()).status_code == 200


class TestARealReelThroughTheQueue:
    """The acceptance criterion's first half: a real render against real fixture media."""

    def test_a_silent_reel_renders_and_delivers_a_real_video(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(
            f"/trips/{trip_id}/reel", json={"aspect": "9:16"}, headers=_headers()
        ).json()["job_id"]
        outcome = env.worker().run_once()
        assert outcome is not None and outcome.state == "succeeded", outcome

        job = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert job["kind"] == "reel"
        assert job["state"] == "succeeded"
        assert job["done"] == job["total"] > 0
        assert job["stage"] == "reel:render"

        response = env.client.get(f"/jobs/{job_id}/reel", headers=_headers())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["immutable"] is True
        assert body["reel_json"]["video"]["aspect"] == "9:16"
        # The control for `test_music_reaches_the_reel_through_ordinary_ingest`: no music_hash was
        # sent, so the render must not claim one was mixed in.
        assert body["reel_json"]["audio"]["music_supplied"] is False

        fetched = httpx.get(body["video"]["download_url"])
        assert fetched.status_code == 200
        assert fetched.headers["content-length"] == str(body["video"]["size_bytes"])

        # **Declared video, actual bytes.** P06 found nine JPEGs under `.mov` names past a schema
        # check and 87 passing presence tests. `file -b`'s ffprobe equivalent:
        probed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                "-",
            ],  # fmt: skip
            input=fetched.content,
            capture_output=True,
        )
        assert b"video" in probed.stdout, probed.stderr

    def test_progress_is_measured_not_invented(self, env, small_media):
        """`done`/`total` come from real cache files on disk, not a fabricated percentage."""
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers()).json()[
            "job_id"
        ]
        worker = env.worker()
        outcome = worker.run_once()
        assert outcome.state == "succeeded"

        paths = trip_paths(env.settings, trip_id)
        cache_dir = paths.out / "reel" / ".cache" / "segments"
        segment_files = list(cache_dir.glob("*.mp4"))
        assert segment_files, "the render must have produced at least one cached segment"

        job = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert job["total"] == len(segment_files)
        assert job["done"] == len(segment_files)
        # No fabricated overall percentage field anywhere in the response, matching the build
        # contract -- checked against the keys, not the prose in progress_basis that explains why
        # there is none.
        assert "percent" not in job and "fraction" not in job

    def test_music_reaches_the_reel_through_ordinary_ingest(self, env, small_media, music_bytes):
        """The task's own requirement: no separate upload path for music."""
        trip_id, _hashes = _built_trip(env, small_media)
        music_hash = hashlib.blake2b(music_bytes).hexdigest()
        declaration = {"hash": music_hash, "filename": "track.m4a", "size": len(music_bytes)}
        negotiated = env.client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={"assets": [declaration]},
            headers=_headers(),
        )
        assert negotiated.status_code == 200, negotiated.text
        needed = negotiated.json()["needed"]
        assert len(needed) == 1
        put = _put(env, needed[0]["put_url"], music_bytes, needed[0]["headers"])
        assert put.status_code == 200, put.text

        job_id = env.client.post(
            f"/trips/{trip_id}/reel", json={"music_hash": music_hash}, headers=_headers()
        ).json()["job_id"]
        outcome = env.worker().run_once()
        assert outcome is not None and outcome.state == "succeeded", outcome

        body = env.client.get(f"/jobs/{job_id}/reel", headers=_headers()).json()
        # `reel.json` names whether music was actually mixed in -- the control against a silent
        # reel is the earlier test, which has no music_hash and must not claim one was used.
        assert body["reel_json"]["audio"]["music_supplied"] is True

        fetched = httpx.get(body["video"]["download_url"])
        probed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                "-",
            ],  # fmt: skip
            input=fetched.content,
            capture_output=True,
        )
        assert b"audio" in probed.stdout, probed.stderr

    def test_a_reel_before_a_build_fails_with_a_clear_reason(self, env, small_media):
        """Media uploaded but never built: `_prepare` succeeds, and `_reel` is what refuses."""
        trip_id = env.client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()[
            "trip_id"
        ]
        _declare_and_upload(env, trip_id, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers()).json()[
            "job_id"
        ]
        outcome = env.worker().run_once()
        assert outcome.state == "failed"
        body = env.client.get(f"/jobs/{job_id}", headers=_headers()).json()
        assert "has not been built yet" in body["error"]

    def test_a_reel_of_a_running_or_queued_job_has_no_download_yet(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(f"/trips/{trip_id}/reel", json={}, headers=_headers()).json()[
            "job_id"
        ]
        response = env.client.get(f"/jobs/{job_id}/reel", headers=_headers())
        assert response.status_code == 409

    def test_a_build_job_id_is_not_a_reel(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        # The build job from `_built_trip` above already succeeded; asking for its "reel" must
        # not be confused with asking for its report.
        build_job_id = env.client.get(f"/trips/{trip_id}/jobs", headers=_headers()).json()["jobs"][
            0
        ]["job_id"]
        response = env.client.get(f"/jobs/{build_job_id}/reel", headers=_headers())
        assert response.status_code == 404


class TestImmutability:
    """D5: a re-cut is a new job with a new job_id, never a mutation of one already handed out."""

    def test_two_reels_of_one_trip_coexist(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        first_job = env.client.post(
            f"/trips/{trip_id}/reel", json={"name": "part-one"}, headers=_headers()
        ).json()["job_id"]
        assert env.worker().run_once().state == "succeeded"

        second_job = env.client.post(
            f"/trips/{trip_id}/reel", json={"name": "part-two"}, headers=_headers()
        ).json()["job_id"]
        assert second_job != first_job
        assert env.worker().run_once().state == "succeeded"

        first = env.client.get(f"/jobs/{first_job}/reel", headers=_headers()).json()
        second = env.client.get(f"/jobs/{second_job}/reel", headers=_headers()).json()
        assert first["video"]["download_url"] != second["video"]["download_url"]
        assert first["reel_json"]["video"]["file"] == "trip.part-one.mp4"
        assert second["reel_json"]["video"]["file"] == "trip.part-two.mp4"

        # Neither file was overwritten by the other: both are still separately fetchable.
        for body in (first, second):
            fetched = httpx.get(body["video"]["download_url"])
            assert fetched.status_code == 200


class TestOptionsReachReelJson:
    """I30's own acceptance criterion, checked from this side of the wire: each option reaches
    the service and is reflected in the returned reel.json."""

    def test_aspect_and_name_are_reflected(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(
            f"/trips/{trip_id}/reel",
            json={"aspect": "1:1", "name": "square-cut"},
            headers=_headers(),
        ).json()["job_id"]
        assert env.worker().run_once().state == "succeeded"
        body = env.client.get(f"/jobs/{job_id}/reel", headers=_headers()).json()
        assert body["reel_json"]["video"]["aspect"] == "1:1"
        # `reel.json` carries no separate "name" field -- the name becomes the filename slug
        # (`export/reel.py:reel_filenames`), which is exactly what a re-cut needs to not collide
        # with another one for the same trip.
        assert body["reel_json"]["video"]["file"] == "trip.square-cut.mp4"

    def test_day_range_narrows_the_render(self, env, small_media):
        trip_id, _hashes = _built_trip(env, small_media)
        trip_json = json.loads((trip_paths(env.settings, trip_id).out / "trip.json").read_text())
        first_day = trip_json["days"][0]["date"]

        job_id = env.client.post(
            f"/trips/{trip_id}/reel", json={"day": first_day}, headers=_headers()
        ).json()["job_id"]
        assert env.worker().run_once().state == "succeeded"
        body = env.client.get(f"/jobs/{job_id}/reel", headers=_headers()).json()
        # A day-only render slugs on the day itself (`ReelSelection.slug`).
        assert body["reel_json"]["video"]["file"] == f"trip.{first_day}.mp4"


@pytest.fixture
def local_env(tmp_path):
    """The local object store backend (S02b) -- what is actually being tested against on-device
    right now, per the task. Same worker, same routes, no S3 anywhere."""
    data_root = tmp_path / "service-data"
    settings = Settings(
        data_root=data_root,
        object_store_backend="local",
        local_store_root=data_root / "objectstore",
        public_base_url="http://127.0.0.1:8000",
        index_dsn=f"sqlite:///{data_root / 'index.db'}",
        worker_inline=False,
        build_no_cloud=True,
        job_heartbeat_s=0.2,
        delivery_presign_ttl_s=120,
    )
    index = SqliteIndex(data_root / "index.db")
    store = LocalFileObjectStore(settings)
    app = create_app(settings, index=index, object_store=store)
    with TestClient(app) as client:
        yield Env(settings, client, index, store)
    index.close()


class TestAgainstTheLocalObjectStore:
    """The same loop, against `LocalFileObjectStore` rather than moto -- a filesystem masquerading
    as S3, which is what a phone on the same Wi-Fi actually talks to today (S02b)."""

    def test_a_music_less_reel_renders_and_delivers_over_the_local_backend(
        self, local_env, small_media
    ):
        env = local_env
        trip_id, _hashes = _built_trip(env, small_media)
        job_id = env.client.post(
            f"/trips/{trip_id}/reel", json={"day": None}, headers=_headers()
        ).json()["job_id"]
        outcome = env.worker().run_once()
        assert outcome is not None and outcome.state == "succeeded", outcome

        body = env.client.get(f"/jobs/{job_id}/reel", headers=_headers()).json()
        # `LocalFileObjectStore`'s "presigned" URL is an ordinary same-app route (app.py), so the
        # TestClient itself can fetch it -- no separate HTTP server needed to prove the bytes.
        download_url = body["video"]["download_url"]
        base = env.settings.public_base_url
        assert download_url.startswith(base), download_url
        fetched = env.client.get(download_url[len(base) :])
        assert fetched.status_code == 200
        probed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                "-",
            ],  # fmt: skip
            input=fetched.content,
            capture_output=True,
        )
        assert b"video" in probed.stdout, probed.stderr
