"""Environment-driven settings.

Read from the environment, never from a checked-in file: the hosting target is still open (see the
iOS tracker's open questions), and every candidate injects configuration as environment variables.
No secret has a default here, and no secret belongs in this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "STORY_SERVICE_"


@dataclass(frozen=True)
class Settings:
    """What the skeleton needs. S02+ add fields; they do not add config files."""

    # Invoked as a subprocess, because that is how the service runs a build. Resolving the CLI by
    # name proves it is on PATH and executable, which importing `story_book` would not.
    story_book_bin: str = "story-book"

    # Trip working directories -- the CLI's `--out`. One POSIX filesystem with real space on it is
    # a hard requirement of the pipeline, and it is the property that rules out request-scoped
    # serverless runtimes.
    data_root: Path = Path("var/service-data")

    # A dependency probe that hangs must fail rather than hold the readiness endpoint open.
    probe_timeout_s: float = 10.0

    # --- S02: the object store -------------------------------------------------------------
    # `s3` (production shape) or `local` (a filesystem masquerading as one, for a same-Wi-Fi
    # phone test before a bucket exists). Explicit rather than inferred from an unset bucket,
    # because inferring it would make a forgotten `S3_BUCKET` silently fall back to local disk
    # instead of failing loudly.
    object_store_backend: str = "s3"

    # The bucket does not exist yet (open question 15), so there is no default and an unset bucket
    # is a configuration error reported at the boundary rather than an AWS error at signing time.
    s3_bucket: str = ""
    s3_region: str = ""
    # Local development points this at MinIO or `moto server`. Unset in production, where the
    # region alone resolves the endpoint and an IAM instance role supplies the credentials --
    # there are deliberately no key fields here.
    s3_endpoint_url: str | None = None
    s3_asset_prefix: str = "assets"

    # Only read when `object_store_backend == "local"`. Bytes land under this directory instead
    # of a bucket; there is no signature, no expiry enforcement, and no cross-host access --
    # this is a same-machine or same-Wi-Fi stand-in, never a deployment shape.
    local_store_root: Path = Path("var/service-data/objectstore")

    # Only read when `object_store_backend == "local"`. There is no bucket to derive a URL from,
    # so a "presigned" URL here just points back at this service -- and this service does not
    # know what host or port a client used to reach it (that lives on the request, not in a
    # constructor), so the base has to be told rather than inferred. An unset value while running
    # local is a configuration error, same shape as an unset bucket for S3.
    public_base_url: str = ""

    # `user` or `content`. See `naming.asset_key` for what each forecloses -- this is open
    # question 4 and it is not settled here.
    asset_scope: str = "user"

    # A presigned PUT is a bearer credential for one key. Long enough for a large clip on hotel
    # wifi, short enough that a leaked URL is not a standing grant.
    presign_ttl_s: int = 3600

    # --- S05: delivery -----------------------------------------------------------------------
    # Where a rendered report bundle and the derived images it points at are staged, once
    # uploaded for delivery. Namespaced apart from `s3_asset_prefix` on purpose: these are
    # pipeline output, not the traveller's own bytes, and deleting a trip's delivery cache must
    # never touch the uploaded originals.
    s3_delivery_prefix: str = "delivery"
    # Shorter than an upload grant: a report bundle or a thumbnail is small, so the cost of a
    # client re-requesting an expired URL is one cheap round trip, not a re-upload.
    delivery_presign_ttl_s: int = 900

    # --- S02: the relational index ---------------------------------------------------------
    # Which engine holds the index is undecided (open question 19). The DSN is the seam: sqlite is
    # implemented for local development and `index.for_dsn` refuses anything else by name rather
    # than falling back to a guess.
    index_dsn: str = ""

    # --- S03: the queue and the worker -----------------------------------------------------
    # The worker runs in a thread of the API process by default. Not because that is the best
    # deployment -- `python -m storybook_service.worker` beside the API is tidier, and the claim
    # transaction makes both correct -- but because the failure mode of the other default is silent:
    # a queued job that nothing ever picks up, on a service whose /health says ok.
    worker_inline: bool = True
    worker_poll_s: float = 1.0

    # How often a running job says it is still alive, and how long a silence has to last before
    # another worker may resume it. The build is minutes to hours, so both are generous; the timeout
    # must stay well above the interval or a slow write requeues a healthy job.
    job_heartbeat_s: float = 10.0
    job_heartbeat_timeout_s: float = 300.0

    # A retry is a resume -- the pipeline caches per item and nothing here wipes `--out`. The limit
    # exists so that a build which is interrupted every single time eventually says so instead of
    # cycling forever.
    job_max_attempts: int = 3

    # Off by default: the pipeline completes with zero network calls either way, and the offline
    # geocoder is what a hosted deployment should be using. On for tests, which must make no
    # network call at all.
    build_no_cloud: bool = False

    def resolved_index_dsn(self) -> str:
        if self.index_dsn:
            return self.index_dsn
        return f"sqlite:///{self.data_root / 'index.db'}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        endpoint = source.get(f"{ENV_PREFIX}S3_ENDPOINT_URL", "")
        return cls(
            story_book_bin=source.get(f"{ENV_PREFIX}STORY_BOOK_BIN", cls.story_book_bin),
            data_root=Path(source.get(f"{ENV_PREFIX}DATA_ROOT", str(cls.data_root))),
            probe_timeout_s=float(
                source.get(f"{ENV_PREFIX}PROBE_TIMEOUT_S", str(cls.probe_timeout_s))
            ),
            object_store_backend=source.get(
                f"{ENV_PREFIX}OBJECT_STORE_BACKEND", cls.object_store_backend
            ),
            s3_bucket=source.get(f"{ENV_PREFIX}S3_BUCKET", cls.s3_bucket),
            s3_region=source.get(f"{ENV_PREFIX}S3_REGION", cls.s3_region),
            s3_endpoint_url=endpoint or None,
            s3_asset_prefix=source.get(f"{ENV_PREFIX}S3_ASSET_PREFIX", cls.s3_asset_prefix),
            local_store_root=Path(
                source.get(f"{ENV_PREFIX}LOCAL_STORE_ROOT", str(cls.local_store_root))
            ),
            public_base_url=source.get(f"{ENV_PREFIX}PUBLIC_BASE_URL", cls.public_base_url),
            asset_scope=source.get(f"{ENV_PREFIX}ASSET_SCOPE", cls.asset_scope),
            presign_ttl_s=int(source.get(f"{ENV_PREFIX}PRESIGN_TTL_S", str(cls.presign_ttl_s))),
            s3_delivery_prefix=source.get(
                f"{ENV_PREFIX}S3_DELIVERY_PREFIX", cls.s3_delivery_prefix
            ),
            delivery_presign_ttl_s=int(
                source.get(f"{ENV_PREFIX}DELIVERY_PRESIGN_TTL_S", str(cls.delivery_presign_ttl_s))
            ),
            index_dsn=source.get(f"{ENV_PREFIX}INDEX_DSN", cls.index_dsn),
            worker_inline=_flag(source.get(f"{ENV_PREFIX}WORKER_INLINE"), cls.worker_inline),
            worker_poll_s=float(source.get(f"{ENV_PREFIX}WORKER_POLL_S", str(cls.worker_poll_s))),
            job_heartbeat_s=float(
                source.get(f"{ENV_PREFIX}JOB_HEARTBEAT_S", str(cls.job_heartbeat_s))
            ),
            job_heartbeat_timeout_s=float(
                source.get(f"{ENV_PREFIX}JOB_HEARTBEAT_TIMEOUT_S", str(cls.job_heartbeat_timeout_s))
            ),
            job_max_attempts=int(
                source.get(f"{ENV_PREFIX}JOB_MAX_ATTEMPTS", str(cls.job_max_attempts))
            ),
            build_no_cloud=_flag(source.get(f"{ENV_PREFIX}BUILD_NO_CLOUD"), cls.build_no_cloud),
        )


def _flag(raw: str | None, default: bool) -> bool:
    """`0`, `false`, `no` and `off` are false; anything else set is true.

    Spelled out because `bool("0")` is `True`, and a deployment that meant to switch the inline
    worker off would have got one anyway.
    """
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")
