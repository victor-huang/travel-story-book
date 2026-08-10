"""The ingest routes, driven over HTTP with a real S3 API behind them.

The acceptance criterion is `TestSecondUploadTransfersNothing`: negotiate, upload for real,
negotiate again, and `needed` is empty. Its control is in the same class -- a first negotiate that
must return *some* hashes as needed, because an endpoint that returned an empty list unconditionally
would pass the criterion and ship a client that uploads nothing.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.index_sqlite import SqliteIndex
from storybook_service.objectstore import S3ObjectStore
from storybook_service.settings import Settings

from tests.conftest import REGION

ME = "traveller@example.com"
YOU = "someone-else@example.com"

BYTES_A = b"the bytes of one photograph"
BYTES_B = b"the bytes of a different photograph, of a different length"
HASH_A = hashlib.blake2b(BYTES_A).hexdigest()
HASH_B = hashlib.blake2b(BYTES_B).hexdigest()


@pytest.fixture
def client(tmp_path, bucket, moto_endpoint):
    settings = Settings(
        data_root=tmp_path / "service-data",
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
    )
    index = SqliteIndex(tmp_path / "index.db")
    app = create_app(settings, index=index, object_store=S3ObjectStore(settings))
    with TestClient(app) as test_client:
        yield test_client
    index.close()


def _as(identity: str) -> dict[str, str]:
    return {"X-Story-Identity": identity}


def _new_trip(client, identity: str = ME, name: str = "Europe 2026") -> str:
    response = client.post("/trips", json={"name": name}, headers=_as(identity))
    assert response.status_code == 201, response.text
    return response.json()["trip_id"]


def _declare(*items) -> dict:
    return {
        "assets": [{"hash": h, "filename": f, "size": len(b)} for h, f, b in items],
    }


def _negotiate(client, trip_id: str, *items, identity: str = ME) -> dict:
    response = client.post(
        f"/trips/{trip_id}/assets:negotiate", json=_declare(*items), headers=_as(identity)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload(entry: dict, body: bytes) -> None:
    response = httpx.put(entry["put_url"], content=body, headers=entry["headers"])
    assert response.status_code == 200, response.text


class TestCreateTrip:
    def test_a_trip_is_created_with_an_id(self, client):
        assert _new_trip(client)

    def test_two_trips_get_different_ids(self, client):
        assert _new_trip(client) != _new_trip(client)

    def test_an_unidentified_caller_is_refused(self, client):
        """It fails closed. A default user is how every trip ends up in one account."""
        assert client.post("/trips", json={"name": "Europe"}).status_code == 401

    def test_no_config_is_scaffolded_at_creation_time(self, client, tmp_path):
        """`story-book init` profiles the media, and at this moment there is none.

        A config written now would record that nothing was measured, and `init` refuses to
        overwrite it later -- so the guess would be permanent.
        """
        trip_id = _new_trip(client)
        trip_dir = tmp_path / "service-data" / "trips" / trip_id
        assert trip_dir.is_dir()
        assert not (trip_dir / "config.toml").exists()

    def test_the_response_says_why_there_is_no_config_yet(self, client):
        body = client.post("/trips", json={}, headers=_as(ME)).json()
        assert "source_config" in body


class TestPerUserIsolation:
    def test_another_users_trip_is_not_readable(self, client):
        trip_id = _new_trip(client, identity=ME)
        assert client.get(f"/trips/{trip_id}", headers=_as(YOU)).status_code == 404

    def test_the_owner_can_read_their_own_trip(self, client):
        """The control for the test above."""
        trip_id = _new_trip(client, identity=ME)
        assert client.get(f"/trips/{trip_id}", headers=_as(ME)).status_code == 200

    def test_another_user_cannot_negotiate_against_the_trip(self, client):
        """Asserted per route, not once. Each route scopes its own read."""
        trip_id = _new_trip(client, identity=ME)
        response = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json=_declare((HASH_A, "IMG_1.jpg", BYTES_A)),
            headers=_as(YOU),
        )
        assert response.status_code == 404

    def test_another_user_cannot_prepare_the_trips_source(self, client):
        trip_id = _new_trip(client, identity=ME)
        assert client.post(f"/trips/{trip_id}/source:prepare", headers=_as(YOU)).status_code == 404

    def test_the_trip_list_shows_only_the_callers_trips(self, client):
        _new_trip(client, identity=ME, name="Mine")
        _new_trip(client, identity=YOU, name="Yours")
        mine = client.get("/trips", headers=_as(ME)).json()["trips"]
        assert [trip["name"] for trip in mine] == ["Mine"]

    def test_the_same_photograph_is_needed_by_each_user_separately(self, client):
        """Per-user key scope, observed from outside: uploading mine does not give you yours.

        This is the trade in open question 4 made visible. Under a content-addressed scope the
        second negotiate would answer `have` -- and the service cannot verify the bytes under a
        hash, so that would let one account decide what another's trip contains.
        """
        mine = _new_trip(client, identity=ME)
        yours = _new_trip(client, identity=YOU)
        first = _negotiate(client, mine, (HASH_A, "IMG_1.jpg", BYTES_A), identity=ME)
        _upload(first["needed"][0], BYTES_A)
        second = _negotiate(client, yours, (HASH_A, "IMG_1.jpg", BYTES_A), identity=YOU)
        assert [entry["hash"] for entry in second["needed"]] == [HASH_A]


class TestSecondUploadTransfersNothing:
    """The acceptance criterion, and the control that makes it mean something."""

    def test_a_first_negotiate_needs_every_asset(self, client):
        """The control. An endpoint that always returned `needed: []` would pass the next test."""
        trip_id = _new_trip(client)
        body = _negotiate(
            client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A), (HASH_B, "IMG_2.jpg", BYTES_B)
        )
        assert {entry["hash"] for entry in body["needed"]} == {HASH_A, HASH_B}
        assert body["have"] == []

    def test_a_second_negotiate_of_an_unchanged_trip_needs_nothing(self, client):
        trip_id = _new_trip(client)
        first = _negotiate(
            client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A), (HASH_B, "IMG_2.jpg", BYTES_B)
        )
        for entry in first["needed"]:
            _upload(entry, BYTES_A if entry["hash"] == HASH_A else BYTES_B)

        second = _negotiate(
            client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A), (HASH_B, "IMG_2.jpg", BYTES_B)
        )
        assert second["needed"] == []
        assert {entry["hash"] for entry in second["have"]} == {HASH_A, HASH_B}

    def test_adding_one_photograph_needs_only_that_photograph(self, client):
        """The reason not to ship a zip, asserted: ten more photos cost ten photos."""
        trip_id = _new_trip(client)
        first = _negotiate(client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A))
        _upload(first["needed"][0], BYTES_A)
        second = _negotiate(
            client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A), (HASH_B, "IMG_2.jpg", BYTES_B)
        )
        assert [entry["hash"] for entry in second["needed"]] == [HASH_B]

    def test_dedup_carries_across_that_users_trips(self, client):
        """One photograph in two trips uploads once -- what the design doc promises for free."""
        first_trip = _new_trip(client, name="Europe")
        second_trip = _new_trip(client, name="Japan")
        first = _negotiate(client, first_trip, (HASH_A, "IMG_1.jpg", BYTES_A))
        _upload(first["needed"][0], BYTES_A)
        second = _negotiate(client, second_trip, (HASH_A, "IMG_1.jpg", BYTES_A))
        assert second["needed"] == []


class TestPresenceIsAMeasurement:
    def test_an_object_of_the_wrong_length_is_treated_as_missing(self, client):
        """The only contradiction available to a service that never reads the bytes.

        A stored length that disagrees with the client's declaration means one of them is wrong, and
        the cheap safe answer is to upload again rather than build from an object nobody can vouch
        for.
        """
        trip_id = _new_trip(client)
        first = _negotiate(client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A))
        # The presigned URL is signed for len(BYTES_A); moto does not enforce that, which is what
        # lets this test put the wrong bytes there at all. On real S3 this upload is refused --
        # which is a better outcome and does not change what is asserted here.
        httpx.put(first["needed"][0]["put_url"], content=BYTES_A + b"tampered")

        second = _negotiate(client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A))
        assert [entry["hash"] for entry in second["needed"]] == [HASH_A]
        assert second["needed"][0]["replaces_mismatched_object"] is True

    def test_a_matching_object_does_not_claim_the_hash_was_verified(self, client):
        """An artifact never overstates its contents, and 'have' is a weaker claim than it looks."""
        trip_id = _new_trip(client)
        body = _negotiate(client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A))
        assert "presence_not_verified" in body["upload"]

    def test_the_response_says_multipart_is_not_implemented(self, client):
        trip_id = _new_trip(client)
        assert (
            _negotiate(client, trip_id, (HASH_A, "IMG_1.jpg", BYTES_A))["upload"]["multipart"]
            is False
        )


class TestFilenamesAndValidation:
    def test_the_filename_is_preserved(self, client):
        """`overrides.toml` addresses by filename, so `IMG_1815.mov` has to stay that."""
        trip_id = _new_trip(client)
        body = _negotiate(client, trip_id, (HASH_A, "IMG_1815.mov", BYTES_A))
        assert body["needed"][0]["filename"] == "IMG_1815.mov"
        assert body["needed"][0]["stored_filename"] == "IMG_1815.mov"
        assert body["needed"][0]["filename_adjusted"] is False

    def test_two_assets_sharing_a_filename_are_both_renamed_and_the_client_is_told(self, client):
        trip_id = _new_trip(client)
        body = _negotiate(
            client, trip_id, (HASH_A, "IMG_0001.JPG", BYTES_A), (HASH_B, "IMG_0001.JPG", BYTES_B)
        )
        stored = {entry["stored_filename"] for entry in body["needed"]}
        assert len(stored) == 2
        assert all(entry["filename_adjusted"] for entry in body["needed"])

    def test_a_collision_arriving_in_a_later_batch_renames_the_earlier_asset_too(self, client):
        """The rename is over the trip's whole asset set, not the current request.

        A per-batch answer could not see the asset already declared, so the first photograph would
        keep the plain name and the second would be the odd one out -- an answer that depends on
        arrival order.
        """
        trip_id = _new_trip(client)
        _negotiate(client, trip_id, (HASH_A, "IMG_0001.JPG", BYTES_A))
        _negotiate(client, trip_id, (HASH_B, "IMG_0001.JPG", BYTES_B))
        assets = client.get(f"/trips/{trip_id}", headers=_as(ME)).json()["assets"]
        stored = {asset["stored_filename"] for asset in assets}
        assert stored == {f"IMG_0001~{HASH_A[:8]}.JPG", f"IMG_0001~{HASH_B[:8]}.JPG"}

    def test_a_traversing_filename_is_refused(self, client):
        trip_id = _new_trip(client)
        response = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={"assets": [{"hash": HASH_A, "filename": "../../evil.jpg", "size": 3}]},
            headers=_as(ME),
        )
        assert response.status_code == 422

    def test_an_asset_id_prefix_is_refused_rather_than_never_matching(self, client):
        trip_id = _new_trip(client)
        response = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={"assets": [{"hash": HASH_A[:12], "filename": "IMG_1.jpg", "size": 3}]},
            headers=_as(ME),
        )
        assert response.status_code == 422

    def test_the_same_hash_twice_in_one_request_is_refused(self, client):
        trip_id = _new_trip(client)
        response = client.post(
            f"/trips/{trip_id}/assets:negotiate",
            json={
                "assets": [
                    {"hash": HASH_A, "filename": "a.jpg", "size": 3},
                    {"hash": HASH_A, "filename": "b.jpg", "size": 9},
                ]
            },
            headers=_as(ME),
        )
        assert response.status_code == 422

    def test_an_empty_negotiate_is_refused(self, client):
        trip_id = _new_trip(client)
        response = client.post(
            f"/trips/{trip_id}/assets:negotiate", json={"assets": []}, headers=_as(ME)
        )
        assert response.status_code == 422


class TestNoMediaCrossesThisService:
    def test_there_is_no_route_that_accepts_media(self, client):
        """Structural, not aspirational.

        The API server must never carry 600 MB, and the way to guarantee that is to have nowhere to
        put it. If a future route takes a body of bytes, this fails and someone has to argue for it.
        """
        paths = client.app.openapi()["paths"]
        bodies = [
            (path, method)
            for path, methods in paths.items()
            for method, spec in methods.items()
            if "multipart/form-data" in (spec.get("requestBody") or {}).get("content", {})
            or "application/octet-stream" in (spec.get("requestBody") or {}).get("content", {})
        ]
        assert bodies == []


class TestObjectStoreUnconfigured:
    def test_negotiate_says_what_to_configure_rather_than_failing_obscurely(self, tmp_path):
        """The bucket does not exist yet, so this is today's normal state for a fresh checkout."""
        settings = Settings(data_root=tmp_path / "d")
        index = SqliteIndex(tmp_path / "index.db")
        app = create_app(settings, index=index)
        with TestClient(app) as unconfigured:
            trip_id = _new_trip(unconfigured)
            response = unconfigured.post(
                f"/trips/{trip_id}/assets:negotiate",
                json=_declare((HASH_A, "IMG_1.jpg", BYTES_A)),
                headers=_as(ME),
            )
        index.close()
        assert response.status_code == 503
        assert "STORY_SERVICE_S3_BUCKET" in response.json()["detail"]

    def test_the_trip_list_still_answers_without_a_bucket(self, tmp_path):
        """An unconfigured store must not take the whole service down."""
        settings = Settings(data_root=tmp_path / "d")
        index = SqliteIndex(tmp_path / "index.db")
        with TestClient(create_app(settings, index=index)) as unconfigured:
            assert unconfigured.get("/trips", headers=_as(ME)).status_code == 200
        index.close()
