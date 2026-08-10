"""The object store, against a real S3 API surface.

The one test that matters is `test_a_presigned_put_round_trips`: a URL this service generated,
`PUT` to over HTTP by something that is not botocore, and then found by `head`. An assertion that a
URL was generated would pass against a URL nobody can use.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import boto3
import httpx
import pytest
from storybook_service.objectstore import ObjectStoreError, S3ObjectStore
from storybook_service.settings import Settings

from tests.conftest import REGION

HASH = hashlib.blake2b(b"a photograph").hexdigest()
BODY = b"the bytes of a photograph"


def _settings(bucket: str, endpoint: str, **kwargs) -> Settings:
    return Settings(s3_bucket=bucket, s3_region=REGION, s3_endpoint_url=endpoint, **kwargs)


@pytest.fixture
def store(bucket, moto_endpoint):
    return S3ObjectStore(_settings(bucket, moto_endpoint))


class TestConfiguration:
    def test_an_unset_bucket_is_a_configuration_error(self, moto_endpoint, s3_credentials):
        """The bucket does not exist yet, so there is none to default to."""
        with pytest.raises(ObjectStoreError, match="S3_BUCKET"):
            S3ObjectStore(Settings(s3_endpoint_url=moto_endpoint))

    def test_settings_carry_no_credential_field(self):
        """IAM instance roles are the intended path, so there is nowhere to put a long-lived key."""
        fields = set(Settings.__dataclass_fields__)
        assert not any("secret" in f or "access_key" in f or "password" in f for f in fields)


class TestHead:
    def test_a_missing_object_is_none(self, store):
        assert store.head("assets/u/nobody/aa/bb/missing") is None

    def test_a_present_object_reports_its_length(self, store, bucket, moto_endpoint):
        client = boto3.client("s3", endpoint_url=moto_endpoint, region_name=REGION)
        client.put_object(Bucket=bucket, Key="k", Body=BODY)
        info = store.head("k")
        assert info is not None and info.size == len(BODY)


class TestPresignPut:
    def test_a_presigned_put_round_trips(self, store, bucket):
        """Generated here, uploaded over HTTP, then found. The whole ingest path in one test."""
        upload = store.presign_put("assets/u/u1/aa/bb/" + HASH, size=len(BODY))
        response = httpx.put(upload.url, content=BODY, headers=upload.headers)
        assert response.status_code == 200, response.text
        info = store.head(upload.key)
        assert info is not None and info.size == len(BODY)

    def test_the_object_is_absent_before_the_put(self, store):
        """The control. Without it, `head` returning a size proves nothing about the upload."""
        upload = store.presign_put("assets/u/u1/aa/bb/" + HASH, size=len(BODY))
        assert store.head(upload.key) is None

    def test_the_url_is_signed(self, store):
        query = parse_qs(urlparse(store.presign_put("k", size=1).url).query)
        assert "X-Amz-Signature" in query
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]

    def test_the_content_length_is_part_of_the_signature(self, store):
        """So the URL is a grant to write exactly this many bytes, not a general write credential.

        S3 enforces this; **moto does not**, so this asserts the URL's shape rather than the
        rejection. A test that PUT the wrong length and expected a 4xx would pass against moto for
        the wrong reason and tell a reader something untrue.
        """
        query = parse_qs(urlparse(store.presign_put("k", size=1234).url).query)
        assert "content-length" in query["X-Amz-SignedHeaders"][0]

    def test_the_declared_length_is_returned_as_a_required_header(self, store):
        assert store.presign_put("k", size=1234).headers == {"Content-Length": "1234"}

    def test_the_expiry_matches_the_configured_ttl(self, bucket, moto_endpoint):
        store = S3ObjectStore(_settings(bucket, moto_endpoint, presign_ttl_s=60))
        upload = store.presign_put("k", size=1)
        query = parse_qs(urlparse(upload.url).query)
        assert query["X-Amz-Expires"] == ["60"]
        # Reported alongside, because a client scheduling retries needs the wall-clock deadline and
        # should not have to parse the URL to find it.
        assert 0 < (upload.expires_at - datetime.now(UTC)).total_seconds() <= 60

    def test_a_negative_size_is_refused(self, store):
        with pytest.raises(ObjectStoreError):
            store.presign_put("k", size=-1)


class TestGetToFile:
    def test_an_object_is_streamed_to_a_path(self, store, bucket, moto_endpoint, tmp_path):
        client = boto3.client("s3", endpoint_url=moto_endpoint, region_name=REGION)
        client.put_object(Bucket=bucket, Key="k", Body=BODY)
        destination = tmp_path / "nested" / "IMG_1.jpg"
        assert store.get_to_file("k", destination) == len(BODY)
        assert destination.read_bytes() == BODY

    def test_a_failed_download_leaves_no_file_under_the_final_name(self, store, tmp_path):
        """The pipeline's resume rests on 'a file that is there is complete'.

        A partial download left under the final name would be scanned as a truncated photograph,
        which is a wrong result rather than a failure.
        """
        destination = tmp_path / "IMG_1.jpg"
        with pytest.raises(ObjectStoreError):
            store.get_to_file("no-such-key", destination)
        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []
