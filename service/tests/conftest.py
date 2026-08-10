"""Shared fixtures: a clean environment, and a real S3 API on localhost.

**Never a real bucket, and never in CI** (open question 15). `moto server` is used rather than
`mock_aws` because a presigned URL is only proven by an HTTP `PUT` made from outside botocore --
which is what the phone does, and what a botocore-level mock cannot observe.

What moto does *not* do, measured when this was written and worth knowing before reading any
assertion below: it verifies **no** signature, **no** expiry and **no** signed `Content-Length`. An
unsigned PUT, a tampered signature and an hour-expired URL all returned `200`. So these tests prove
the round trip and the URL's shape; enforcement is a property of S3 and is not measured here.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto.server import ThreadedMotoServer

BUCKET = "storybook-test-bucket"
REGION = "eu-central-1"


@pytest.fixture(autouse=True)
def _no_env_bleed(monkeypatch):
    """A developer's own STORY_SERVICE_* variables must not decide what these tests measure."""
    for suffix in (
        "STORY_BOOK_BIN",
        "DATA_ROOT",
        "PROBE_TIMEOUT_S",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ENDPOINT_URL",
        "S3_ASSET_PREFIX",
        "ASSET_SCOPE",
        "PRESIGN_TTL_S",
        "INDEX_DSN",
    ):
        monkeypatch.delenv(f"STORY_SERVICE_{suffix}", raising=False)


@pytest.fixture(scope="session")
def moto_endpoint():
    """One in-process S3 for the session. Buckets are per-test, so tests stay independent."""
    server = ThreadedMotoServer(port=0)
    server.start()
    _host, port = server.get_host_and_port()
    # 127.0.0.1 explicitly: moto binds 0.0.0.0 and a presigned URL built against that host is
    # signed for a host name the client may not be able to reach.
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def s3_credentials(monkeypatch):
    """boto3 needs *some* credentials to sign with; these reach nothing.

    In production the credential is an IAM instance role and there is no key to configure, which is
    why `Settings` has no field for one.
    """
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "testing"


@pytest.fixture
def bucket(moto_endpoint, s3_credentials, request):
    """A fresh bucket per test, named after the test so a leak is traceable."""
    name = f"{BUCKET}-{abs(hash(request.node.nodeid)) % 10**8}"
    client = boto3.client("s3", endpoint_url=moto_endpoint, region_name=REGION)
    client.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    yield name
