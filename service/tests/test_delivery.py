"""S05 end to end: a real build through the queue, then a real fetch of what it delivers.

**Look at real output** (CLAUDE.md): every assertion below either unzips the bundle it downloaded
or decodes the bytes it fetched, rather than trusting a 200 status or a schema. `small_media`
deliberately includes a video, so the poster path -- the one Q12 warned a naive bundle silently
drops -- has a real file to check.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
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

from tests.conftest import REGION

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "media"
# A still and a video: the video exercises the poster path, which is the one Q12 says a naive
# bundle silently drops.
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


@dataclass
class Env:
    settings: Settings
    client: TestClient
    index: SqliteIndex
    store: S3ObjectStore

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


def _upload_and_build(env: Env, media: dict[str, bytes]) -> tuple[str, str]:
    """Create a trip, upload every asset, and return `(trip_id, job_id)` -- not yet run."""
    trip_id = env.client.post("/trips", json={"name": "Salzburg"}, headers=_headers()).json()[
        "trip_id"
    ]
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
        put = httpx.put(entry["put_url"], content=by_hash[entry["hash"]], headers=entry["headers"])
        assert put.status_code == 200, put.text
    job_id = env.client.post(f"/trips/{trip_id}/build", headers=_headers()).json()["job_id"]
    return trip_id, job_id


def _succeeded(env: Env, media: dict[str, bytes]) -> tuple[str, str]:
    trip_id, job_id = _upload_and_build(env, media)
    outcome = env.worker().run_once()
    assert outcome is not None and outcome.state == "succeeded", outcome
    return trip_id, job_id


class TestTheReportBundle:
    def test_a_job_still_running_has_no_bundle_yet(self, env, small_media):
        _trip_id, job_id = _upload_and_build(env, small_media)
        response = env.client.get(f"/jobs/{job_id}/report", headers=_headers())
        assert response.status_code == 409
        assert "queued" in response.json()["detail"] or "running" in response.json()["detail"]

    def test_the_bundle_is_a_real_zip_containing_only_html_css_and_vendor(self, env, small_media):
        """Design (I25): storyasset:// media refs mean no thumbs/, previews/ or .cache/ inside."""
        _trip_id, job_id = _succeeded(env, small_media)
        response = env.client.get(f"/jobs/{job_id}/report", headers=_headers())
        assert response.status_code == 200, response.text
        bundle = response.json()["bundle"]
        assert bundle["media_rel"] == "storyasset://"

        fetched = httpx.get(bundle["download_url"])
        assert fetched.status_code == 200
        assert fetched.headers["content-length"] == str(bundle["size_bytes"])

        archive = zipfile.ZipFile(io.BytesIO(fetched.content))
        names = set(archive.namelist())
        assert "report/index.html" in names
        assert "report/style.css" in names
        assert "report/vendor/leaflet.js" in names
        assert "report/vendor/leaflet.css" in names
        # The control: a naive bundle (I24's original shape) would carry these. This one must not.
        assert not any(n.startswith("report/thumbs/") for n in names)
        assert not any(n.startswith("report/previews/") for n in names)
        assert not any(".cache" in n for n in names)

        index_html = archive.read("report/index.html").decode()
        assert "storyasset://thumbs/" in index_html

    def test_a_second_request_does_not_re_render(self, env, small_media):
        """Immutable per job: the zip is built once, and a re-request is a cheap re-presign."""
        _trip_id, job_id = _succeeded(env, small_media)
        first = env.client.get(f"/jobs/{job_id}/report", headers=_headers()).json()
        paths = trip_paths(env.settings, first["trip_id"])
        zip_path = paths.trip_dir / "delivery" / job_id / "report.zip"
        mtime_before = zip_path.stat().st_mtime_ns

        second = env.client.get(f"/jobs/{job_id}/report", headers=_headers()).json()
        assert zip_path.stat().st_mtime_ns == mtime_before
        # Still a fresh, working grant even though nothing was rebuilt.
        refetched = httpx.get(second["bundle"]["download_url"])
        assert refetched.status_code == 200

    def test_a_degraded_build_says_so_on_the_bundle_response_too(self, env, small_media):
        """The bundle is complete for what the pipeline computed; degradation is still surfaced."""
        _trip_id, job_id = _succeeded(env, small_media)
        body = env.client.get(f"/jobs/{job_id}/report", headers=_headers()).json()
        assert body["degraded"] is True
        assert any(entry["stage"] == "embeddings" for entry in body["unavailable_stages"])

    def test_media_summary_is_a_real_measurement(self, env, small_media):
        _trip_id, job_id = _succeeded(env, small_media)
        body = env.client.get(f"/jobs/{job_id}/report", headers=_headers()).json()
        summary = body["media_summary"]
        assert summary["total"] == len(small_media)
        assert 0 < summary["with_thumbnail"] <= summary["total"]

    def test_another_account_cannot_fetch_the_bundle(self, env, small_media):
        _trip_id, job_id = _succeeded(env, small_media)
        response = env.client.get(
            f"/jobs/{job_id}/report", headers=_headers("someone@else.example")
        )
        assert response.status_code == 404


class TestMediaByRelpath:
    def _relpaths(self, env: Env, trip_id: str) -> dict[str, dict]:
        paths = trip_paths(env.settings, trip_id)
        doc = json.loads((paths.out / "trip.json").read_text())
        return doc["assets"]

    def test_a_still_thumbnail_resolves_to_real_jpeg_bytes(self, env, small_media):
        trip_id, _job_id = _succeeded(env, small_media)
        assets = self._relpaths(env, trip_id)
        still = next(a for a in assets.values() if a["kind"] != "video" and a.get("thumbnail"))

        response = env.client.get(
            f"/trips/{trip_id}/media/{still['thumbnail']}",
            headers=_headers(),
            follow_redirects=False,
        )
        assert response.status_code == 302
        fetched = httpx.get(response.headers["location"])
        assert fetched.status_code == 200
        # Identity, not presence (CLAUDE.md's "verify one file's actual bytes"): a real JPEG.
        assert fetched.content[:2] == b"\xff\xd8"

    def test_a_video_poster_resolves_too(self, env, small_media):
        """The exact case Q12 is about: a bundle assembled from the obvious roots loses this."""
        trip_id, _job_id = _succeeded(env, small_media)
        assets = self._relpaths(env, trip_id)
        video = next(a for a in assets.values() if a["kind"] == "video")
        assert video.get("thumbnail"), "the fixture needs a poster, or this test proves nothing"

        response = env.client.get(
            f"/trips/{trip_id}/media/{video['thumbnail']}",
            headers=_headers(),
            follow_redirects=False,
        )
        assert response.status_code == 302
        fetched = httpx.get(response.headers["location"])
        assert fetched.status_code == 200
        assert fetched.content[:2] == b"\xff\xd8"

    def test_a_path_trip_json_does_not_claim_is_refused(self, env, small_media):
        """Never a path a client merely asks for -- the control for the whitelist above."""
        trip_id, _job_id = _succeeded(env, small_media)
        response = env.client.get(
            f"/trips/{trip_id}/media/thumbs/does-not-exist.jpg", headers=_headers()
        )
        assert response.status_code == 404

    def test_path_traversal_outside_out_dir_is_refused(self, env, small_media):
        trip_id, _job_id = _succeeded(env, small_media)
        response = env.client.get(f"/trips/{trip_id}/media/../../../etc/passwd", headers=_headers())
        assert response.status_code == 404

    def test_another_account_cannot_fetch_this_trips_media(self, env, small_media):
        trip_id, _job_id = _succeeded(env, small_media)
        assets = self._relpaths(env, trip_id)
        still = next(a for a in assets.values() if a.get("thumbnail"))
        response = env.client.get(
            f"/trips/{trip_id}/media/{still['thumbnail']}",
            headers=_headers("someone@else.example"),
        )
        assert response.status_code == 404
