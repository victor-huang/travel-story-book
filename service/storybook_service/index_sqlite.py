"""A SQLite implementation of `Index`, for local development.

**This file is not `story.db` and must never be confused with it.** `story.db` is one file per
trip, owned by the pipeline, with a single-row `trip` table; this is one file for the whole
deployment, owned by the service, holding what that file structurally cannot. The pipeline's rule
against raw SQL applies to *its* tables, not to these.

Written straight against `sqlite3` rather than an ORM because the point of the exercise is that a
Postgres version is a day's work, not that this one is elaborate. The queue lives here too (S03):
its two atomic operations -- claiming a job, and refusing a second active job for one trip -- are
the two things a caller cannot arrange by being careful, so they are a transaction and a partial
unique index rather than advice.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from storybook_service.index import (
    IDENTITY_KINDS,
    JOB_KINDS,
    JOB_PHASES,
    IndexError_,
    Job,
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

-- One queued unit of work. `owner_id` is deliberately *not* a column here: it is reached through
-- `trip`, so a job cannot be asked for without naming whose trip it belongs to, and the two can
-- never disagree.
CREATE TABLE IF NOT EXISTS job (
    id            TEXT PRIMARY KEY,
    trip_id       TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    phase         TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    heartbeat_at  TEXT,
    worker_id     TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    exit_code     INTEGER,
    capability    TEXT NOT NULL DEFAULT '',
    -- S07: `options` is the client's own request (a build's is always ''; a reel's carries
    -- aspect/day-range/music/name/subtitles, since unlike a build there can be many reels for one
    -- trip and each needs its own). `progress` is the worker's own measurement, written once
    -- before the expensive part starts -- a reel touches no database, so build's live story.db
    -- read has nothing to read for it, and this is where "never invented" progress has to live.
    options       TEXT NOT NULL DEFAULT '',
    progress      TEXT NOT NULL DEFAULT ''
);

-- **"One worker per trip at a time", enforced by the schema.** The design doc asks for the queue to
-- serialise concurrent builds of one trip rather than for code to defend against them, and a
-- partial unique index is the smallest way to make the bad state unrepresentable: a trip may have
-- at most one job that is queued or running. A second build request therefore returns the existing
-- job instead of racing it.
CREATE UNIQUE INDEX IF NOT EXISTS job_one_active_per_trip
    ON job(trip_id) WHERE state IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS job_queue ON job(state, created_at);
CREATE INDEX IF NOT EXISTS job_by_trip ON job(trip_id, created_at);
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


def _parse_opt(ts: str | None) -> datetime | None:
    return None if ts is None else _parse(ts)


class SqliteIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # **WAL is mandatory, not tuning** (question 19, answered). S03's worker writes job
        # progress on one connection while the API reads it on another, in another process, over
        # one filesystem; WAL is what makes one writer plus many readers safe there. Without it a
        # poll during a heartbeat write is an SQLITE_BUSY, and the endpoint that reports progress
        # would fail exactly while there is progress to report. `:memory:` has no journal to set.
        if str(self.path) != ":memory:":
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode.lower() != "wal":  # pragma: no cover - a filesystem that refuses WAL
                raise IndexError_(
                    f"could not put {self.path} into WAL mode (got {mode!r}). The API and the "
                    "worker are separate connections to this file; question 19 makes WAL a "
                    "requirement rather than a preference."
                )
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

    # --- S03: jobs -------------------------------------------------------------------------

    JOB_COLUMNS = (
        "j.id, j.trip_id, t.owner_id, j.kind, j.state, j.phase, j.attempts, j.created_at, "
        "j.started_at, j.finished_at, j.heartbeat_at, j.worker_id, j.error, j.exit_code, "
        "j.capability, j.options, j.progress"
    )

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            trip_id=row["trip_id"],
            owner_id=row["owner_id"],
            kind=row["kind"],
            state=row["state"],
            phase=row["phase"],
            attempts=row["attempts"],
            created_at=_parse(row["created_at"]),
            started_at=_parse_opt(row["started_at"]),
            finished_at=_parse_opt(row["finished_at"]),
            heartbeat_at=_parse_opt(row["heartbeat_at"]),
            worker_id=row["worker_id"],
            error=row["error"],
            exit_code=row["exit_code"],
            capability=row["capability"],
            options=row["options"],
            progress=row["progress"],
        )

    def _active_job_for_trip(self, trip_id: str) -> Job | None:
        row = self._conn.execute(
            f"SELECT {self.JOB_COLUMNS} FROM job j JOIN trip t ON t.id = j.trip_id "
            "WHERE j.trip_id = ? AND j.state IN ('queued', 'running')",
            (trip_id,),
        ).fetchone()
        return None if row is None else self._job(row)

    def enqueue_job(
        self, *, owner_id: str, trip_id: str, kind: str, job_id: str, options: str = ""
    ) -> tuple[Job, bool]:
        if kind not in JOB_KINDS:
            raise IndexError_(f"job kind must be one of {JOB_KINDS}; got {kind!r}")
        existing = self._active_job_for_trip(trip_id)
        if existing is not None:
            return existing, False
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO job (id, trip_id, kind, state, created_at, options) "
                    "VALUES (?, ?, ?, 'queued', ?, ?)",
                    (job_id, trip_id, kind, _now(), options),
                )
        except sqlite3.IntegrityError:
            # The partial unique index fired: another request queued one between the SELECT above
            # and this INSERT. The right answer is that request's job, not an error -- the caller
            # asked for a build of this trip and there is going to be one.
            racer = self._active_job_for_trip(trip_id)
            if racer is None:  # pragma: no cover - would mean the index fired for another reason
                raise
            return racer, False
        job = self.get_job(owner_id=owner_id, job_id=job_id)
        if job is None:  # pragma: no cover - the insert just succeeded for this owner's trip
            raise IndexError_(f"job {job_id} vanished between insert and read")
        return job, True

    def get_job(self, *, owner_id: str, job_id: str) -> Job | None:
        # The join carries the ownership check, so there is no way to read a job without naming
        # whose it is. A route cannot forget what it cannot express.
        row = self._conn.execute(
            f"SELECT {self.JOB_COLUMNS} FROM job j JOIN trip t ON t.id = j.trip_id "
            "WHERE j.id = ? AND t.owner_id = ?",
            (job_id, owner_id),
        ).fetchone()
        return None if row is None else self._job(row)

    def list_jobs(self, *, owner_id: str, trip_id: str) -> list[Job]:
        rows = self._conn.execute(
            f"SELECT {self.JOB_COLUMNS} FROM job j JOIN trip t ON t.id = j.trip_id "
            "WHERE j.trip_id = ? AND t.owner_id = ? ORDER BY j.created_at DESC, j.id",
            (trip_id, owner_id),
        ).fetchall()
        return [self._job(row) for row in rows]

    def claim_next_job(self, *, worker_id: str, now: datetime) -> Job | None:
        stamp = now.isoformat()
        # BEGIN IMMEDIATE takes the write lock before the SELECT, so two workers cannot both read
        # the same queued row and then both update it. Under WAL a reader is never blocked by this.
        # Managed by hand rather than with `with self._conn:` because that context manager commits
        # a transaction it did not open, and an early `return None` inside it would leave the write
        # lock held until the next statement.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT j.id FROM job j WHERE j.state = 'queued' "
                # Redundant today -- the partial unique index already forbids a queued job on a
                # trip that has one running -- and kept because "one worker per trip at a time" is
                # the invariant, and it should not depend on one index definition to survive.
                "  AND NOT EXISTS (SELECT 1 FROM job r WHERE r.trip_id = j.trip_id "
                "                  AND r.state = 'running') "
                "ORDER BY j.created_at, j.id LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return None
            self._conn.execute(
                "UPDATE job SET state = 'running', worker_id = ?, phase = 'prepare', "
                "  attempts = attempts + 1, started_at = COALESCE(started_at, ?), "
                "  heartbeat_at = ? WHERE id = ?",
                (worker_id, stamp, stamp, row["id"]),
            )
            claimed = self._conn.execute(
                f"SELECT {self.JOB_COLUMNS} FROM job j JOIN trip t ON t.id = j.trip_id "
                "WHERE j.id = ?",
                (row["id"],),
            ).fetchone()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self._job(claimed)

    def heartbeat_job(self, *, job_id: str, phase: str, at: datetime) -> None:
        if phase not in JOB_PHASES:
            raise IndexError_(f"job phase must be one of {JOB_PHASES}; got {phase!r}")
        with self._conn:
            self._conn.execute(
                "UPDATE job SET heartbeat_at = ?, phase = ? WHERE id = ?",
                (at.isoformat(), phase, job_id),
            )

    def record_job_capability(self, *, job_id: str, capability: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE job SET capability = ? WHERE id = ?", (capability, job_id))

    def record_job_progress(self, *, job_id: str, progress: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE job SET progress = ? WHERE id = ?", (progress, job_id))

    def finish_job(
        self,
        *,
        job_id: str,
        state: str,
        at: datetime,
        error: str = "",
        exit_code: int | None = None,
    ) -> None:
        if state not in ("succeeded", "failed"):
            raise IndexError_(f"a job finishes 'succeeded' or 'failed'; got {state!r}")
        if state == "failed" and not error:
            # A failure with no reason is the artifact overstating itself in the other direction:
            # the client can see that it broke and never what broke.
            raise IndexError_("a failed job must carry the reason it failed")
        with self._conn:
            self._conn.execute(
                "UPDATE job SET state = ?, phase = '', finished_at = ?, error = ?, exit_code = ? "
                "WHERE id = ?",
                (state, at.isoformat(), error, exit_code, job_id),
            )

    def requeue_job(self, *, job_id: str, at: datetime, error: str = "") -> None:
        with self._conn:
            self._conn.execute(
                # `attempts` is left where it is: the attempt happened. `started_at` is left too,
                # so the record still says when the work first began. Nothing under `--out` is
                # touched, which is what makes the retry a resume.
                "UPDATE job SET state = 'queued', phase = '', worker_id = '', heartbeat_at = ?, "
                "  error = ? WHERE id = ?",
                (at.isoformat(), error, job_id),
            )

    def stale_running_jobs(self, *, before: datetime) -> list[Job]:
        rows = self._conn.execute(
            f"SELECT {self.JOB_COLUMNS} FROM job j JOIN trip t ON t.id = j.trip_id "
            "WHERE j.state = 'running' AND (j.heartbeat_at IS NULL OR j.heartbeat_at < ?)",
            (before.isoformat(),),
        ).fetchall()
        return [self._job(row) for row in rows]

    def queued_ahead(self, *, job_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM job WHERE state = 'queued' AND (created_at, id) < "
            "  (SELECT created_at, id FROM job WHERE id = ?)",
            (job_id,),
        ).fetchone()
        return int(row["n"])

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
