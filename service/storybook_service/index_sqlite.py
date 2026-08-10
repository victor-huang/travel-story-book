"""A SQLite implementation of `Index`, for local development.

**This file is not `story.db` and must never be confused with it.** `story.db` is one file per
trip, owned by the pipeline, with a single-row `trip` table; this is one file for the whole
deployment, owned by the service, holding what that file structurally cannot. The pipeline's rule
against raw SQL applies to *its* tables, not to these.

Written straight against `sqlite3` rather than an ORM because the interface is five methods and
the point of the exercise is that a Postgres version is a day's work, not that this one is
elaborate.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from storybook_service.index import (
    IDENTITY_KINDS,
    IndexError_,
    Trip,
    TripAsset,
    User,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

-- An email address *or* a phone number identifies a user (question 17), and Google today with
-- Apple later means one user accumulates several identities over time. Hence a table rather than
-- columns on `user`: adding 'apple' must not be a migration of every row.
CREATE TABLE IF NOT EXISTS identity (
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS trip (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trip_owner ON trip(owner_id, created_at);

-- What a trip claims to contain. `stored_filename` is stored rather than recomputed on read so that
-- one place decides it and the materialised folder can be reconciled against it -- a later
-- negotiate that renames a colliding pair updates these rows, and `materialise_source` removes the
-- files left under the old names.
CREATE TABLE IF NOT EXISTS trip_asset (
    trip_id          TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    media_hash       TEXT NOT NULL,
    filename         TEXT NOT NULL,
    stored_filename  TEXT NOT NULL,
    size             INTEGER NOT NULL,
    declared_at      TEXT NOT NULL,
    PRIMARY KEY (trip_id, media_hash)
);
"""


def new_id() -> str:
    """Unguessable, and not derived from insertion order.

    A trip id appears in every URL the client uses, so a sequential one would let a caller walk
    other accounts' trips even before the query-level scoping is trusted. It is also not a
    published *pipeline* identifier -- nothing in `trip.json` is keyed by it -- so randomness costs
    nothing there.
    """
    return secrets.token_urlsafe(16)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class SqliteIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def ensure_user(self, *, kind: str, value: str) -> User:
        if kind not in IDENTITY_KINDS:
            raise IndexError_(f"identity kind must be one of {IDENTITY_KINDS}; got {kind!r}")
        if not value:
            raise IndexError_("an identity needs a value")
        row = self._conn.execute(
            "SELECT u.id, u.created_at FROM identity i JOIN user u ON u.id = i.user_id "
            "WHERE i.kind = ? AND i.value = ?",
            (kind, value),
        ).fetchone()
        if row is not None:
            return User(id=row["id"], created_at=_parse(row["created_at"]))
        user_id, now = new_id(), _now()
        with self._conn:
            self._conn.execute("INSERT INTO user (id, created_at) VALUES (?, ?)", (user_id, now))
            self._conn.execute(
                "INSERT INTO identity (kind, value, user_id, created_at) VALUES (?, ?, ?, ?)",
                (kind, value, user_id, now),
            )
        return User(id=user_id, created_at=_parse(now))

    def create_trip(self, *, owner_id: str, name: str, trip_id: str) -> Trip:
        now = _now()
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO trip (id, owner_id, name, created_at) VALUES (?, ?, ?, ?)",
                    (trip_id, owner_id, name, now),
                )
        except sqlite3.IntegrityError as exc:
            raise IndexError_(f"cannot create trip for owner {owner_id!r}: {exc}") from exc
        return Trip(id=trip_id, owner_id=owner_id, name=name, created_at=_parse(now))

    def get_trip(self, *, owner_id: str, trip_id: str) -> Trip | None:
        # `owner_id` is in the WHERE clause, not applied by the caller afterwards. A route that
        # forgets to check ownership cannot exist, because there is no way to ask without it.
        row = self._conn.execute(
            "SELECT id, owner_id, name, created_at FROM trip WHERE id = ? AND owner_id = ?",
            (trip_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return Trip(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            created_at=_parse(row["created_at"]),
        )

    def list_trips(self, *, owner_id: str) -> list[Trip]:
        rows = self._conn.execute(
            "SELECT id, owner_id, name, created_at FROM trip WHERE owner_id = ? "
            "ORDER BY created_at DESC, id",
            (owner_id,),
        ).fetchall()
        return [
            Trip(
                id=row["id"],
                owner_id=row["owner_id"],
                name=row["name"],
                created_at=_parse(row["created_at"]),
            )
            for row in rows
        ]

    def record_assets(self, *, trip_id: str, assets: list[TripAsset]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO trip_asset "
                "  (trip_id, media_hash, filename, stored_filename, size, declared_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                # A re-negotiation is the normal case -- the client retries, or adds ten photos to
                # a trip -- so the same asset arriving twice updates rather than raising.
                "ON CONFLICT (trip_id, media_hash) DO UPDATE SET "
                "  filename = excluded.filename, "
                "  stored_filename = excluded.stored_filename, "
                "  size = excluded.size",
                [
                    (
                        trip_id,
                        asset.media_hash,
                        asset.filename,
                        asset.stored_filename,
                        asset.size,
                        asset.declared_at.isoformat(),
                    )
                    for asset in assets
                ],
            )

    def trip_assets(self, *, trip_id: str) -> list[TripAsset]:
        rows = self._conn.execute(
            "SELECT media_hash, filename, stored_filename, size, declared_at "
            "FROM trip_asset WHERE trip_id = ? ORDER BY media_hash",
            (trip_id,),
        ).fetchall()
        return [
            TripAsset(
                media_hash=row["media_hash"],
                filename=row["filename"],
                stored_filename=row["stored_filename"],
                size=row["size"],
                declared_at=_parse(row["declared_at"]),
            )
            for row in rows
        ]
