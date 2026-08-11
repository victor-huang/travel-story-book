"""The relational index: users, their identities, their trips, and which assets a trip has.

`story.db` is one file per trip with `CHECK (id = 1)` on its `trip` table, so it structurally
cannot hold a list of trips, a user, or a job. Open question 17 answered what fills that hole -- a
`user` identified by an email address or a phone number, and a one-to-many `user` -> `trip`
relation -- but **not which engine holds it.** Postgres on RDS is the obvious fit for the EC2
target; SQLite on an EBS volume is defensible for a single instance.

So this module is an interface and a DSN, deliberately narrow enough that the second
implementation is small: no query language at the boundary, no ORM, and no type crossing it that
is not a `str`, an `int`, a `datetime` or a dataclass of those. `index_sqlite` is the one
implementation, chosen because it needs nothing installed for local development. `for_dsn` refuses
an unimplemented scheme **by name** rather than falling back, because a silent fallback to sqlite
on a production host is the failure mode this seam exists to make impossible.

The seam was six methods when S02 wrote it; S03's queue adds ten, and the property that matters is
unchanged. Two of them earn their place at the boundary rather than in a caller: `enqueue_job` and
`claim_next_job` are the only atomic operations here, because "one active job per trip" and "two
workers never take one job" are not things a caller can arrange by being careful.

One rule is not negotiable at this boundary and is why `owner_id` is a parameter of every read
rather than something a caller filters afterwards: **a user may only ever see their own trips, and
that is enforced in the query.** These are someone's family photographs; a route that forgot to
filter would return another account's trip and no test of that route would notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

IDENTITY_KINDS = ("email", "phone", "google", "apple")

JOB_KINDS = ("build",)
"""`reel` joins this in S07. One kind today, named rather than implied."""

JOB_STATES = ("queued", "running", "succeeded", "failed")
"""Four states, and the distinction between the last two is not cosmetic.

A build that exited non-zero, or exited zero and wrote no `trip.json`, is `failed` and says why.
It must never read like a job that finished, however much of the work it happened to complete.
"""

JOB_PHASES = ("", "prepare", "build")
"""Which half of the job is running. Empty until a worker claims it.

`prepare` fetches the uploaded media onto the filesystem and scaffolds the config; `build` is the
CLI. Both report real counts, from different sources -- see `progress.py`.
"""


class IndexError_(Exception):
    """A request the index refuses."""


class UnknownIndexEngine(IndexError_):
    """The DSN names an engine that has no implementation here."""


@dataclass(frozen=True, slots=True)
class User:
    id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Trip:
    id: str
    owner_id: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TripAsset:
    """One asset a trip claims, as the client declared it.

    `size` is the client's declaration, not a measurement the service took -- it is used to
    presign a length-bound PUT and to notice a stored object whose length disagrees. The service
    never reads the bytes, so it cannot confirm that they hash to `media_hash`; nothing here
    should be read as if it had.
    """

    media_hash: str
    filename: str
    stored_filename: str
    size: int
    declared_at: datetime


@dataclass(frozen=True, slots=True)
class Job:
    """One queued unit of work, and the unit is **the trip**.

    T58 is the documented cost of letting the transport decide the unit: a package split per day
    came back as three one-day `story.json` files. Uploads are per asset (S02); a job is per trip,
    and there is no route or column here that can express a fraction of one.

    Nothing on this record is an estimate. `phase` and the counts a caller sees come from the
    pipeline's own `stage_result` table or from the materialised file count -- never from elapsed
    time, and never smoothed.
    """

    id: str
    trip_id: str
    owner_id: str
    kind: str
    state: str
    phase: str
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    """Last time the running worker said it was still alive.

    A worker killed by an OOM or a deploy leaves a row saying `running` forever otherwise, and the
    job would neither progress nor retry. Staleness is measured against this, and a stale job is
    **requeued, not failed** -- the pipeline caches per item, so the retry resumes.
    """
    worker_id: str
    error: str
    exit_code: int | None
    capability: str
    """JSON: which stages this deployment could run, measured at this job's start.

    Empty until a worker measures it. Read from each stage's own `available()`, so a degraded build
    (no `clip`, say) is declared in the words of the stage that stopped working rather than
    inferred from an absence.
    """


@runtime_checkable
class Index(Protocol):
    """Everything S02 and S03 need from a database, and nothing more.

    S07 adds reels. They belong on this interface too; they are absent because nothing needs them
    yet and an unused method is an untested one.
    """

    def ensure_user(self, *, kind: str, value: str) -> User:
        """Find or create the user holding this identity.

        An identity is `(kind, value)` -- an email address or a phone number today (question 17),
        a Google or Apple subject once S06 exists -- and it is unique across users.
        """

    def create_trip(self, *, owner_id: str, name: str, trip_id: str) -> Trip: ...

    def get_trip(self, *, owner_id: str, trip_id: str) -> Trip | None:
        """None both when the trip does not exist and when it belongs to someone else.

        Deliberately indistinguishable: a 404 that differs from a 403 tells a caller which trip
        ids exist.
        """

    def list_trips(self, *, owner_id: str) -> list[Trip]: ...

    def record_assets(self, *, trip_id: str, assets: list[TripAsset]) -> None:
        """Idempotent by `(trip_id, media_hash)`. A re-negotiated asset is not a second row."""

    def trip_assets(self, *, trip_id: str) -> list[TripAsset]: ...

    # --- S03: jobs -------------------------------------------------------------------------

    def enqueue_job(
        self, *, owner_id: str, trip_id: str, kind: str, job_id: str
    ) -> tuple[Job, bool]:
        """Queue a job for this trip, or hand back the one already queued or running.

        The bool is "created". **One active job per trip** is the design's requirement, and it is
        enforced by the store rather than by a caller checking first: `story.db` has a single-row
        `trip` table, so two concurrent builds of one trip are not a shape the pipeline supports.
        A second request is therefore idempotent rather than an error -- the client that lost its
        `job_id` gets it back.
        """

    def get_job(self, *, owner_id: str, job_id: str) -> Job | None:
        """None both when the job does not exist and when its trip belongs to someone else.

        Same reason as `get_trip`: a caller must not be able to learn which job ids exist.
        """

    def list_jobs(self, *, owner_id: str, trip_id: str) -> list[Job]:
        """Newest first. The client that was killed mid-build finds its job here."""

    def claim_next_job(self, *, worker_id: str, now: datetime) -> Job | None:
        """Atomically take the oldest queued job whose trip has nothing running.

        The claim is a single transaction because two workers must not take one job -- which is
        also what makes an in-process worker and a separate worker process the same code.
        """

    def heartbeat_job(self, *, job_id: str, phase: str, at: datetime) -> None: ...

    def record_job_capability(self, *, job_id: str, capability: str) -> None: ...

    def finish_job(
        self,
        *,
        job_id: str,
        state: str,
        at: datetime,
        error: str = "",
        exit_code: int | None = None,
    ) -> None: ...

    def requeue_job(self, *, job_id: str, at: datetime, error: str = "") -> None:
        """Put a claimed job back in the queue without touching its output directory.

        Used for an interrupt and for a worker that died. `--out` is deliberately left alone: every
        stage result is cached by content hash and committed per item, so the retry resumes and
        loses at most one item's work. Wiping it would defeat the only performance guarantee this
        project makes.
        """

    def stale_running_jobs(self, *, before: datetime) -> list[Job]:
        """Jobs that say `running` but whose worker stopped reporting."""

    def queued_ahead(self, *, job_id: str) -> int:
        """How many queued jobs were created before this one. A count, not a time estimate."""

    def close(self) -> None: ...


def for_dsn(dsn: str) -> Index:
    """The only place an engine is chosen, so swapping it is one function and one module."""
    if dsn.startswith("sqlite:///"):
        from storybook_service.index_sqlite import SqliteIndex

        return SqliteIndex(dsn.removeprefix("sqlite:///"))
    scheme = dsn.split("://", 1)[0] if "://" in dsn else dsn
    raise UnknownIndexEngine(
        f"no index implementation for {scheme!r}. Only 'sqlite:///<path>' is implemented; which "
        "engine holds the index in production is an open question awaiting a human "
        "(Postgres on RDS vs. SQLite on EBS). Implement it against storybook_service.index.Index "
        "rather than widening callers."
    )
