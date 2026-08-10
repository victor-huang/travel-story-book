"""The object store: presign a PUT, ask whether an object is there, fetch it for a build.

**The API server never carries the media.** A trip is ~600 MB and a zip of it has no resume, no
early start, and re-sends everything to add ten photos -- so uploads go straight to the store on a
presigned `PUT` and this module's upload path issues a URL and nothing else. `get_to_file` exists
for the opposite direction, where the *service* is the client: a build needs the bytes on a real
filesystem, and that transfer is server-to-store, not phone-to-server.

What this module can and cannot promise, stated here because the tests cannot say it:

- A presigned URL is signed for one bucket, one key, one method and one content length, and S3
  enforces all four. **`moto` enforces none of them** -- an unsigned PUT, a tampered signature and
  an expired URL all returned `200` against `moto server` when this was written. So the suite
  proves the round trip and the URL's shape; it does not and cannot prove enforcement. Anything
  that depends on enforcement is a claim about S3, not a measurement taken here.
- `head` reports the length the store holds. It does **not** confirm that those bytes hash to the
  key they are under: the service never reads them, by design. A length that disagrees with the
  client's declaration is the one cheap contradiction available, and `Ingest` treats it as "not
  present" so the asset is re-uploaded rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from storybook_service.settings import Settings


class ObjectStoreError(Exception):
    """The store could not answer, or is not configured."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    key: str
    url: str
    method: str
    expires_at: datetime
    headers: dict[str, str]
    """Headers the client must send for the signature to verify.

    Returned rather than documented, because a signature over `content-length` that the client
    does not reproduce fails at S3 with a message about the signature and no hint about why.
    """


class ObjectStore(Protocol):
    def head(self, key: str) -> ObjectInfo | None: ...

    def presign_put(self, key: str, *, size: int) -> PresignedUpload: ...

    def get_to_file(self, key: str, destination: Path) -> int: ...


class S3ObjectStore:
    """S3, or anything speaking it -- MinIO and `moto server` for local development.

    There are no credential fields: in production the credential is an IAM instance role, which
    boto3 resolves from the instance metadata service with nothing configured. Long-lived keys are
    not a supported shape here, so there is no place to put one.
    """

    def __init__(self, settings: Settings, client=None) -> None:
        if not settings.s3_bucket:
            raise ObjectStoreError(
                "STORY_SERVICE_S3_BUCKET is unset. The bucket is configuration, never a default: "
                "there is no bucket this service may assume it owns."
            )
        self.bucket = settings.s3_bucket
        self.ttl_s = settings.presign_ttl_s
        self._client = client or boto3.client(
            "s3",
            region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url,
            # SigV4 explicitly: the default for a region-less client can still be SigV2-ish for
            # some endpoints, and a v2 presigned URL will not verify against a v4-only bucket.
            config=Config(signature_version="s3v4"),
        )

    def head(self, key: str) -> ObjectInfo | None:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (403, 404):
                # 403 is folded in with 404 deliberately: a bucket policy that denies HEAD on a
                # missing key answers 403, and treating that as "present" would skip an upload.
                return None
            raise ObjectStoreError(f"HEAD {key}: {exc}") from exc
        return ObjectInfo(key=key, size=int(response["ContentLength"]))

    def presign_put(self, key: str, *, size: int) -> PresignedUpload:
        if size < 0:
            raise ObjectStoreError(f"size must not be negative; got {size}")
        # ContentLength is signed, so the URL is a grant to write exactly this many bytes at this
        # key -- not a general write credential. S3 rejects a mismatch; see the module docstring
        # for why the local suite cannot demonstrate that.
        params = {"Bucket": self.bucket, "Key": key, "ContentLength": size}
        issued_at = datetime.now(UTC)
        url = self._client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=self.ttl_s, HttpMethod="PUT"
        )
        return PresignedUpload(
            key=key,
            url=url,
            method="PUT",
            expires_at=issued_at + timedelta(seconds=self.ttl_s),
            headers={"Content-Length": str(size)},
        )

    def get_to_file(self, key: str, destination: Path) -> int:
        """Stream one object to a path, atomically.

        Atomic because the pipeline's resumability rests on "a file that is there is complete":
        a partial download left under the final name would be scanned as a truncated photograph,
        and that is a wrong result rather than a failure. Same reasoning as I15's ledger.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + ".partial")
        try:
            with staging.open("wb") as handle:
                self._client.download_fileobj(self.bucket, key, handle)
        except ClientError as exc:
            staging.unlink(missing_ok=True)
            raise ObjectStoreError(f"GET {key}: {exc}") from exc
        staging.replace(destination)
        return destination.stat().st_size
