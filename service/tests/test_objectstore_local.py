"""`LocalFileObjectStore`, and the routes its "presigned" URLs point at.

Not a deployment shape -- a same-Wi-Fi stand-in so an upload/build/report loop can be tested
before a bucket exists (open question 15). The one property that matters here is different from
`S3ObjectStore`'s tests: there is no signature to fail to enforce, so the thing worth proving is
path-traversal rejection, not signature shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.objectstore import LocalFileObjectStore, ObjectStoreError
from storybook_service.settings import Settings


def _settings(tmp_path: Path, base_url: str = "http://192.168.1.23:8000") -> Settings:
    return Settings(
        object_store_backend="local",
        local_store_root=tmp_path / "objectstore",
        public_base_url=base_url,
    )


class TestConstruction:
    def test_requires_a_public_base_url(self, tmp_path: Path) -> None:
        with pytest.raises(ObjectStoreError, match="PUBLIC_BASE_URL"):
            LocalFileObjectStore(_settings(tmp_path, base_url=""))

    def test_creates_its_root_directory(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        assert not settings.local_store_root.exists()
        LocalFileObjectStore(settings)
        assert settings.local_store_root.is_dir()


class TestDirectCalls:
    """The `ObjectStore` protocol methods, called directly -- what the worker uses for
    `get_to_file`/`put_file`, not what the phone uses."""

    def test_head_is_none_before_a_write_and_present_after(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(_settings(tmp_path))
        assert store.head("users/a/photo.heic") is None

        source = tmp_path / "source.bin"
        source.write_bytes(b"eleven bytes")
        store.put_file("users/a/photo.heic", source)

        info = store.head("users/a/photo.heic")
        assert info is not None
        assert info.size == len(b"eleven bytes")

    def test_get_to_file_round_trips_the_exact_bytes(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(_settings(tmp_path))
        payload = b"\x00\x01real bytes, not text\xff"
        source = tmp_path / "source.bin"
        source.write_bytes(payload)
        store.put_file("k", source)

        destination = tmp_path / "roundtrip.bin"
        size = store.get_to_file("k", destination)
        assert size == len(payload)
        assert destination.read_bytes() == payload
        # The atomic-write staging file must not survive.
        assert not destination.with_name(destination.name + ".partial").exists()

    def test_get_to_file_missing_key_raises(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(_settings(tmp_path))
        with pytest.raises(ObjectStoreError):
            store.get_to_file("never-written", tmp_path / "out.bin")

    @pytest.mark.parametrize("bad_key", ["../escape", "a/../../b", "/absolute", "a//", ""])
    def test_path_traversal_keys_are_rejected(self, tmp_path: Path, bad_key: str) -> None:
        store = LocalFileObjectStore(_settings(tmp_path))
        with pytest.raises(ObjectStoreError):
            store.path_for(bad_key)


class TestPresignedShapes:
    def test_presign_put_points_at_the_local_route_under_the_base_url(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(_settings(tmp_path, base_url="http://10.0.0.5:8000"))
        grant = store.presign_put("users/a/x.jpg", size=42)
        assert grant.url == "http://10.0.0.5:8000/_local-object-store/users/a/x.jpg"
        assert grant.method == "PUT"
        assert grant.headers["Content-Length"] == "42"

    def test_presign_get_carries_the_filename_as_a_query_param(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(_settings(tmp_path))
        grant = store.presign_get("delivery/x/report.zip", filename="report.zip")
        assert grant.url.endswith("/_local-object-store/delivery/x/report.zip?filename=report.zip")


class TestRoutesEndToEnd:
    """Through a real `TestClient`, PUT then GET -- the path the phone actually takes, not the
    worker's direct-call path above."""

    def test_put_then_get_returns_the_same_bytes(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings, index=object(), object_store=None)
        with TestClient(app) as client:
            payload = b"a whole photograph's worth of bytes, pretend"
            put = client.put("/_local-object-store/users/a/photo.heic", content=payload)
            assert put.status_code == 200

            got = client.get("/_local-object-store/users/a/photo.heic")
            assert got.status_code == 200
            assert got.content == payload

    def test_get_of_a_key_never_written_is_404(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings, index=object(), object_store=None)
        with TestClient(app) as client:
            resp = client.get("/_local-object-store/never/written")
            assert resp.status_code == 404

    def test_get_honours_the_filename_query_param_as_content_disposition(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings, index=object(), object_store=None)
        with TestClient(app) as client:
            client.put("/_local-object-store/delivery/x/report.zip", content=b"zip-bytes")
            resp = client.get(
                "/_local-object-store/delivery/x/report.zip", params={"filename": "report.zip"}
            )
            assert resp.status_code == 200
            assert "report.zip" in resp.headers.get("content-disposition", "")

    def test_path_traversal_through_the_route_cannot_escape_the_root(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings, index=object(), object_store=None)
        canary = tmp_path / "canary.txt"
        canary.write_text("should never be overwritten")
        with TestClient(app) as client:
            # FastAPI/Starlette's :path converter itself normalizes ".." out of routed paths,
            # so this is really a defense-in-depth check on `path_for`'s own rejection, exercised
            # here through the route rather than skipped because the framework happens to help.
            resp = client.put("/_local-object-store/../canary.txt", content=b"attacker bytes")
            assert resp.status_code in (404, 400, 422)
        assert canary.read_text() == "should never be overwritten"

    def test_the_s3_backend_registers_no_local_routes(self, tmp_path: Path) -> None:
        """Control: these routes must not exist at all for the production backend -- an S3
        deployment carrying a route that reads/writes arbitrary local files would be a real
        vulnerability, even an unused one."""
        settings = Settings(object_store_backend="s3")
        app = create_app(settings, index=object(), object_store=None)
        with TestClient(app) as client:
            resp = client.get("/_local-object-store/anything")
            assert resp.status_code == 404
