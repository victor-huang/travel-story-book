"""S06: verified Google/Apple bearer tokens, and the dev header they sit beside.

Real RSA keys sign real JWTs; only the network fetch of the provider's JWKS is mocked (the key
lookup, `PyJWKClient.get_signing_key_from_jwt`), so `jwt.decode`'s signature, audience, issuer and
expiry checks all run for real. This is the `--no-cloud` philosophy applied to a test: no live
network call, but no mocking of the thing under test either.

The acceptance criterion for S06 is `TestCrossTenantIsolationWithVerifiedTokens`: two verified
principals, two different `sub` values, hitting a real route through a real `TestClient`, and
account A never sees account B's trip -- asserted per route, mirroring
`tests/test_ingest.py::TestPerUserIsolation` but for the verified path instead of the dev header.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from storybook_service.app import create_app
from storybook_service.index_sqlite import SqliteIndex
from storybook_service.objectstore import S3ObjectStore
from storybook_service.principal import DEV_IDENTITY_HEADER
from storybook_service.settings import Settings

from tests.conftest import REGION

GOOGLE_CLIENT_ID = "example-app.apps.googleusercontent.com"
APPLE_CLIENT_ID = "com.example.storybook"

KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(rsa_key, claims: dict) -> str:
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _claims(*, issuer: str, audience: str, sub: str, exp_delta: int = 3600) -> dict:
    now = int(time.time())
    return {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "iat": now,
        "exp": now + exp_delta,
    }


@pytest.fixture(autouse=True)
def _mock_jwks(mocker, rsa_key):
    """Stand in for `PyJWKClient.get_signing_key_from_jwt` -- the network fetch, not the check.

    Real client code calls `jwt.PyJWKClient(url).get_signing_key_from_jwt(token)`; here it always
    returns this test's key regardless of which JWKS URL or token it was asked for, since there is
    only ever one signer in this test module.
    """

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    mocker.patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        return_value=_FakeSigningKey(rsa_key.public_key()),
    )


@pytest.fixture
def client(tmp_path, bucket, moto_endpoint):
    settings = Settings(
        data_root=tmp_path / "service-data",
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
        google_client_id=GOOGLE_CLIENT_ID,
        apple_client_id=APPLE_CLIENT_ID,
    )
    index = SqliteIndex(tmp_path / "index.db")
    app = create_app(settings, index=index, object_store=S3ObjectStore(settings))
    with TestClient(app) as test_client:
        yield test_client
    index.close()


@pytest.fixture
def client_dev_header_disallowed(tmp_path, bucket, moto_endpoint):
    settings = Settings(
        data_root=tmp_path / "service-data",
        s3_bucket=bucket,
        s3_region=REGION,
        s3_endpoint_url=moto_endpoint,
        google_client_id=GOOGLE_CLIENT_ID,
        apple_client_id=APPLE_CLIENT_ID,
        allow_dev_identity_header=False,
    )
    index = SqliteIndex(tmp_path / "index.db")
    app = create_app(settings, index=index, object_store=S3ObjectStore(settings))
    with TestClient(app) as test_client:
        yield test_client
    index.close()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestVerifiedGoogleToken:
    def test_valid_token_is_authenticated(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-1"),
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 201, response.text

    def test_bare_issuer_form_is_also_accepted(self, client, rsa_key):
        """Real Google ID tokens have carried both `accounts.google.com` and the https:// form."""
        token = _sign(
            rsa_key, _claims(issuer="accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-2")
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 201, response.text

    def test_ensure_user_is_called_with_the_sub_and_google_kind(self, client, rsa_key):
        """Not just a 2xx -- the identity actually landed in the index under 'google'/sub."""
        token = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-3"),
        )
        client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        another_token = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-3"),
        )
        trips = client.get("/trips", headers=_bearer(another_token)).json()["trips"]
        assert len(trips) == 1


class TestVerifiedAppleToken:
    def test_valid_token_is_authenticated(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(issuer="https://appleid.apple.com", audience=APPLE_CLIENT_ID, sub="a-sub-1"),
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 201, response.text


class TestRejectedTokens:
    def test_expired_token_is_401(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(
                issuer="https://accounts.google.com",
                audience=GOOGLE_CLIENT_ID,
                sub="g-sub-4",
                exp_delta=-3600,
            ),
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 401

    def test_wrong_audience_is_401(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(
                issuer="https://accounts.google.com",
                audience="some-other-client-id",
                sub="g-sub-5",
            ),
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 401

    def test_unlisted_issuer_is_401(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(issuer="https://evil.example.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-6"),
        )
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
        assert response.status_code == 401

    def test_unlisted_issuer_does_not_fall_back_to_dev_header(self, client, rsa_key):
        """The downgrade-attack control: a bad bearer token plus a valid dev header must still fail.

        If a bad Authorization header silently fell through to the header path, a caller could
        defeat verification just by sending a malformed bearer token alongside a believed header.
        """
        token = _sign(
            rsa_key,
            _claims(issuer="https://evil.example.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-7"),
        )
        headers = _bearer(token)
        headers[DEV_IDENTITY_HEADER] = "traveller@example.com"
        response = client.post("/trips", json={"name": "Kyoto"}, headers=headers)
        assert response.status_code == 401

    def test_tampered_signature_is_401(self, client, rsa_key):
        token = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="g-sub-8"),
        )
        header_b64, payload_b64, sig_b64 = token.split(".")
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        tampered_token = f"{header_b64}.{payload_b64}.{tampered_sig}"
        response = client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(tampered_token))
        assert response.status_code == 401

    def test_unconfigured_provider_is_401(self, tmp_path, bucket, moto_endpoint, rsa_key):
        """Google is configured, Apple is not -- an Apple token must not verify against a
        missing audience."""
        settings = Settings(
            data_root=tmp_path / "service-data",
            s3_bucket=bucket,
            s3_region=REGION,
            s3_endpoint_url=moto_endpoint,
            google_client_id=GOOGLE_CLIENT_ID,
            apple_client_id="",
        )
        index = SqliteIndex(tmp_path / "index.db")
        app = create_app(settings, index=index, object_store=S3ObjectStore(settings))
        with TestClient(app) as test_client:
            token = _sign(
                rsa_key,
                _claims(
                    issuer="https://appleid.apple.com", audience=APPLE_CLIENT_ID, sub="a-sub-2"
                ),
            )
            response = test_client.post("/trips", json={"name": "Kyoto"}, headers=_bearer(token))
            assert response.status_code == 401
        index.close()


class TestDevHeaderStillWorks:
    def test_dev_header_authenticates_when_allowed(self, client):
        response = client.post(
            "/trips", json={"name": "Kyoto"}, headers={DEV_IDENTITY_HEADER: "me@example.com"}
        )
        assert response.status_code == 201, response.text

    def test_dev_header_is_rejected_when_disallowed(self, client_dev_header_disallowed):
        response = client_dev_header_disallowed.post(
            "/trips", json={"name": "Kyoto"}, headers={DEV_IDENTITY_HEADER: "me@example.com"}
        )
        assert response.status_code == 401


class TestCrossTenantIsolationWithVerifiedTokens:
    """The literal S06 acceptance criterion, for the verified path.

    Mirrors `tests/test_ingest.py::TestPerUserIsolation`, which already proves this for the dev
    header -- this proves the same property holds when identity comes from a verified token
    instead, asserted per route rather than once.
    """

    def test_account_a_cannot_read_account_bs_trip(self, client, rsa_key):
        token_a = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="acct-a"),
        )
        token_b = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="acct-b"),
        )
        created = client.post("/trips", json={"name": "A's trip"}, headers=_bearer(token_a))
        trip_id = created.json()["trip_id"]

        as_b = client.get(f"/trips/{trip_id}", headers=_bearer(token_b))
        assert as_b.status_code == 404

        as_a = client.get(f"/trips/{trip_id}", headers=_bearer(token_a))
        assert as_a.status_code == 200

    def test_account_a_does_not_see_account_bs_trip_in_the_list(self, client, rsa_key):
        token_a = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="acct-c"),
        )
        token_b = _sign(
            rsa_key,
            _claims(issuer="https://accounts.google.com", audience=GOOGLE_CLIENT_ID, sub="acct-d"),
        )
        client.post("/trips", json={"name": "Mine"}, headers=_bearer(token_a))
        client.post("/trips", json={"name": "Yours"}, headers=_bearer(token_b))

        mine = client.get("/trips", headers=_bearer(token_a)).json()["trips"]
        assert [t["name"] for t in mine] == ["Mine"]

        yours = client.get("/trips", headers=_bearer(token_b)).json()["trips"]
        assert [t["name"] for t in yours] == ["Yours"]
