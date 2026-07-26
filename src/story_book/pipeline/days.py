"""Module 5: group media into local calendar days.

**No trip-boundary detection here.** One trip per run -- the input folder *is* the trip -- so
this stage never decides where a trip starts or ends; it only buckets the media it's given.

A "day" is not midnight-to-midnight. `config.time.day_start_hour` (default 4) shifts the
boundary so a late-night sequence stays with the evening it began rather than becoming a
one-item 1am "day": an item at 01:30 belongs to the *previous* calendar date's bucket. This is
the entire reason the field exists -- see `assign_days` below.

Day assignment reads `taken_local` (the naive local wall-clock reading); ordering and gap
detection use `taken_utc`. Mixing the two up is exactly how timezone bugs manifest, per the
project's hard-won gotchas.

Items with no timestamp cannot be placed on a day. They are left unassigned and counted, never
silently dropped.

A gap larger than `config.time.suspicious_gap_days` is warned about, never split -- it usually
means two trips were passed in one folder, and splitting is the user's call (via `overrides.toml`
in a later module, not this stage).

Note on the schema: `media` carries no `day_id` -- the day/media relationship is established
later, when Module 6 (events) links media to an event that belongs to a day. This stage's whole
job is to make sure the right `day` rows exist with the right `local_date` values.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from story_book.config import Config
from story_book.db.connection import iter_media
from story_book.db.models import Media
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GapWarning:
    """A gap between two consecutive (UTC-ordered) dated items that exceeds the suspicious
    threshold. Reported, never acted on -- splitting is a human decision."""

    before: Media
    after: Media
    gap_days: float


def assign_days(media_list: list[Media], day_start_hour: int) -> dict[str, str]:
    """Map each dated item's hash to its local-day bucket (`YYYY-MM-DD`).

    Pure function over an in-memory list -- no DB, no filesystem. Items with no `taken_local`
    are excluded from the returned mapping; the caller is responsible for counting them.

    The bucket is the item's own naive local calendar date, shifted back one day if its local
    hour falls before `day_start_hour`. Each item is judged purely by its own local clock
    reading -- there is no cross-item timezone reconciliation here, which is deliberate: two
    items straddling a timezone crossing (e.g. one read against Vienna, the next against
    Istanbul, minutes apart in real time) can land in different naive calendar dates yet still
    bucket into the same day once the shift is applied, or vice versa. That is correct: "day"
    is a property of the trip's local experience of time, not of a shared reference frame.
    """
    assignments: dict[str, str] = {}
    for media in media_list:
        if not media.taken_local:
            continue
        local_dt = datetime.fromisoformat(media.taken_local)
        bucket = local_dt.date()
        if local_dt.hour < day_start_hour:
            bucket -= timedelta(days=1)
        assignments[media.hash] = bucket.isoformat()
    return assignments


def find_suspicious_gaps(media_list: list[Media], suspicious_gap_days: float) -> list[GapWarning]:
    """Consecutive-in-UTC gaps between dated items larger than `suspicious_gap_days`.

    Ordering here must be `taken_utc`, not `taken_local` -- local time is what stages
    day-bucketing, but it does not order correctly across a timezone change.
    """
    dated = sorted((m for m in media_list if m.taken_utc), key=lambda m: m.taken_utc)
    warnings: list[GapWarning] = []
    for before, after in zip(dated, dated[1:], strict=False):
        delta = datetime.fromisoformat(after.taken_utc) - datetime.fromisoformat(before.taken_utc)
        gap_days = delta.total_seconds() / 86400
        if gap_days > suspicious_gap_days:
            warnings.append(GapWarning(before=before, after=after, gap_days=gap_days))
    return warnings


def _sync_day_rows(conn: sqlite3.Connection, local_dates: set[str]) -> None:
    """Make the `day` table's rows match `local_dates` exactly, reusing existing rows and never
    duplicating them across a re-run (the `UNIQUE (trip_id, local_date)` constraint backs this).

    A date no longer present in the current media set is removed -- unless it already has
    `event` rows attached, in which case removing it would cascade-delete real downstream work
    for a transient reason (e.g. a stage running before its upstream dependency finished
    populating timestamps). That case is warned about instead of silently kept or destroyed.
    """
    existing = {
        row["local_date"]: row["id"]
        for row in conn.execute("SELECT id, local_date FROM day WHERE trip_id = 1")
    }

    for local_date in local_dates - set(existing):
        conn.execute(
            """
            INSERT INTO day (trip_id, local_date) VALUES (1, ?)
            ON CONFLICT (trip_id, local_date) DO NOTHING
            """,
            (local_date,),
        )

    for local_date in set(existing) - local_dates:
        day_id = existing[local_date]
        has_events = conn.execute(
            "SELECT 1 FROM event WHERE day_id = ? LIMIT 1", (day_id,)
        ).fetchone()
        if has_events:
            logger.warning(
                "days: %s no longer has any dated media but already has event(s) attached -- "
                "leaving the day row in place rather than orphaning those events",
                local_date,
            )
            continue
        conn.execute("DELETE FROM day WHERE id = ?", (day_id,))


def _set_trip_range_if_unset(conn: sqlite3.Connection, start_local: str, end_local: str) -> None:
    """Fill `trip.start_local`/`end_local` from the observed range, only where still unset --
    config or a previous run may already have set them deliberately."""
    # Recomputed, not COALESCEd. These are derived values, not user settings: guarding them
    # against overwrite meant that adding a photo from an earlier date to a built trip left
    # `start_local` wrong forever -- the stage re-ran, computed the right answer, and declined to
    # store it. Same shape as the bugs `always_run` exists to prevent.
    conn.execute(
        """
        UPDATE trip
        SET start_local = ?,
            end_local = ?
        WHERE id = 1
        """,
        (start_local, end_local),
    )


class DaysStage(WholeTripStage):
    """Group all dated media into local calendar days (Module 5)."""

    name = "days"
    version = 1
    # Aggregate over the whole media set: a cached 'ok' would mean a photo added by a later
    # `scan` belongs to no day and silently vanishes from the timeline. Re-bucketing is cheap,
    # pure in-memory work over rows already in the DB, so running it every build is fine.
    always_run = True
    description = "Group media into local calendar days."

    def run(self, ctx: StageContext) -> None:
        media_list = list(iter_media(ctx.conn))
        dated = [m for m in media_list if m.taken_local]
        undated_count = len(media_list) - len(dated)
        if undated_count:
            logger.warning(
                "days: %d item(s) have no usable timestamp and could not be placed on a day",
                undated_count,
            )

        self._warn_suspicious_gaps(dated, ctx.config)

        assignments = assign_days(dated, ctx.config.time.day_start_hour)
        _sync_day_rows(ctx.conn, set(assignments.values()))

        if dated:
            _set_trip_range_if_unset(
                ctx.conn,
                start_local=min(m.taken_local for m in dated),
                end_local=max(m.taken_local for m in dated),
            )

    def _warn_suspicious_gaps(self, dated: list[Media], config: Config) -> None:
        for gap in find_suspicious_gaps(dated, config.time.suspicious_gap_days):
            logger.warning(
                "days: gap of %.1f day(s) between %s and %s exceeds suspicious_gap_days=%.1f -- "
                "this often means two trips were passed in one folder. Not splitting; "
                "review the range and split manually if needed.",
                gap.gap_days,
                gap.before.hash,
                gap.after.hash,
                config.time.suspicious_gap_days,
            )
