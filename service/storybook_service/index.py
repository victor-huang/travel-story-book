"""The relational index: users, their identities, their trips, and which assets a trip has.

`story.db` is one file per trip with `CHECK (id = 1)` on its `trip` table, so it structurally
cannot hold a list of trips, a user, or a job. Open question 17 answered what fills that hole -- a
`user` identified by an email address or a phone number, and a one-to-many `user` -> `trip`
relation -- but **not which engine holds it.** Postgres on RDS is the obvious fit for the EC2
target; SQLite on an EBS volume is defensible for a single instance.

So this module is an interface and a DSN, deliberately narrow enough that the second
implementation is small: five methods, no query language at the boundary, no ORM. `index_sqlite`
is the one implementation, chosen because it needs nothing installed for local development.
`for_dsn` refuses an unimplemented scheme **by name** rather than falling back, because a silent
fallback to sqlite on a production host is the failure mode this seam exists to make impossible.

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


@runtime_checkable
class Index(Protocol):
    """Everything S02 needs from a database, and nothing more.

    S03 adds jobs and S07 adds reels. They belong on this interface too; they are absent because
    S02 does not need them and an unused method is an untested one.
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
