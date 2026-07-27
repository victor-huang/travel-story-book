"""Module 6: event detection.

Turns a day's flat, time-ordered media into the *stops* that make up its story -- a morning at a
palace, an afternoon in a cathedral, an evening concert. Almost everything downstream is scoped by
event: near-duplicate clustering runs within an event, selection picks highlights per event,
landmark recognition sends one representative per event, and the timeline is a list of them. An
event boundary that is wrong is wrong four more times before it reaches the reader.

Two design points are load-bearing, and both were paid for.

**No landmark labels as input.** The original plan had event detection consuming landmark names
while landmark recognition ran three modules later -- a circular dependency. Events are split on
time and position only. Landmark labels may *name* or *refine* an event on a later pass; they never
create one.

**Events are internal, and that is a deliberate decision backed by measurement.** They scope
deduplication, selection and landmark sampling. They are *not* the chapters a reader sees; those
are proposed by the AI from the contact sheets and edited in `overrides.toml`.

P03 settled this. Hand-labelled chapter boundaries on a real day fell after a **2-minute** gap and
an **8-minute** gap, while the pipeline's fell at 17-57 minutes -- *anti-correlated*. Measured
against the nearest previous point in the same event, those boundaries were **10 metres** and
**230 metres** apart, while ordinary within-event movement reached **2.8 km**. So the boundaries
are an order of magnitude closer together than normal movement inside an event: the photographer
was standing still, walking out of one building and into another.

A grid search over gap, jump and duration could not exceed F1 57%. Adding CLIP content distance
made it *worse* (33%): the three real boundaries sit at cosine distances 0.74/0.66/0.57 against a
within-event median of 0.32 -- top tail, but 27 of 154 within-event pairs are at least as distant.

The conclusion is not that the thresholds need more work. The information is not in the metadata
or in visual similarity: "this is the concert we came for" is knowledge about the trip. So the
pipeline stops trying to infer it, produces honest time-and-location clusters, and leaves
semantics to the human and the model.

There is no maximum-duration rule. One was added earlier to stop a user-facing "event" spanning
nine hours -- on a diagnosis measurement later disproved -- and a long cluster is harmless now
that clusters are internal. It is in fact *safer* for deduplication: near-duplicates can only be
found within a cluster, so under-splitting costs some comparisons while over-splitting loses
duplicates outright.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt

from story_book.config import Config, EventConfig
from story_book.db.connection import iter_media
from story_book.db.models import Media
from story_book.overrides import ResolvedOverrides, resolve
from story_book.pipeline.base import StageContext, WholeTripStage
from story_book.pipeline.days import assign_days

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0


@dataclass(slots=True)
class DetectedEvent:
    """One event before it becomes a row: the members plus what the row will be derived from."""

    local_date: str
    seq: int
    members: list[Media]

    @property
    def start_utc(self) -> str | None:
        return self.members[0].taken_utc if self.members else None

    @property
    def end_utc(self) -> str | None:
        return self.members[-1].taken_utc if self.members else None

    @property
    def centroid(self) -> tuple[float | None, float | None]:
        located = [m for m in self.members if m.lat is not None and m.lon is not None]
        if not located:
            return None, None
        return (
            sum(m.lat for m in located) / len(located),
            sum(m.lon for m in located) / len(located),
        )

    @property
    def place_id(self) -> int | None:
        """The place most of the members resolved to.

        A majority vote rather than the first member's place: an event that begins while walking
        can have its first photo resolve to a neighbouring cell.
        """
        places = Counter(m.place_id for m in self.members if m.place_id is not None)
        return places.most_common(1)[0][0] if places else None


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (*first, *second))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    inner = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(inner))


def _minutes_between(earlier: Media, later: Media) -> float:
    if not earlier.taken_utc or not later.taken_utc:
        return 0.0
    delta = datetime.fromisoformat(later.taken_utc) - datetime.fromisoformat(earlier.taken_utc)
    return delta.total_seconds() / 60.0


def _centroid(members: list[Media]) -> tuple[float, float] | None:
    located = [m for m in members if m.lat is not None and m.lon is not None]
    if not located:
        return None
    return (
        sum(m.lat for m in located) / len(located),
        sum(m.lon for m in located) / len(located),
    )


def starts_new_event(
    current: list[Media], candidate: Media, config: EventConfig | Config
) -> tuple[bool, str]:
    """Whether `candidate` begins a new event, and which rule decided it.

    Returning the reason keeps the decision inspectable -- when a real day splits oddly, knowing
    *which* rule fired is the difference between tuning the right threshold and guessing.
    """
    events = config.events if isinstance(config, Config) else config
    if not current:
        return False, ""

    previous = current[-1]
    if _minutes_between(previous, candidate) > events.gap_minutes:
        return True, "time_gap"

    if candidate.lat is not None and candidate.lon is not None:
        centroid = _centroid(current)
        if centroid is not None:
            distance = haversine_km(centroid, (candidate.lat, candidate.lon))
            if distance > events.jump_km:
                return True, "gps_jump"

    return False, ""


def detect_events(
    media_list: list[Media], config: Config, overrides: ResolvedOverrides | None = None
) -> list[DetectedEvent]:
    """Split each day's media into events. Pure: no DB, no filesystem.

    Undated items are excluded -- they cannot be placed in a chronology, and inventing a position
    for them would put a photo in a story it may not belong to. The caller counts them.
    """
    overrides = overrides or ResolvedOverrides()
    day_by_hash = assign_days(media_list, config.time.day_start_hour)
    dated = [m for m in media_list if m.hash in day_by_hash and m.taken_utc]
    dated.sort(key=lambda m: (m.taken_utc or "", m.hash))

    by_day: dict[str, list[Media]] = {}
    for media in dated:
        by_day.setdefault(day_by_hash[media.hash], []).append(media)

    events: list[DetectedEvent] = []
    for local_date in sorted(by_day):
        current: list[Media] = []
        seq = 1
        for media in by_day[local_date]:
            if media.hash in overrides.split_before:
                split, reason = bool(current), "override"
            else:
                split, reason = starts_new_event(current, media, config)
            if split:
                events.append(DetectedEvent(local_date, seq, current))
                logger.debug("event %s#%d ended: %s", local_date, seq, reason)
                seq += 1
                current = [media]
            else:
                current.append(media)
        if current:
            events.append(DetectedEvent(local_date, seq, current))
    return apply_merges(events, overrides)


def apply_merges(events: list[DetectedEvent], overrides: ResolvedOverrides) -> list[DetectedEvent]:
    """Join the events named by each `merge_events` group into one, then renumber each day.

    A merge names *photos*, and the events holding them are joined. Events between two named
    ones are swept in too: a merge that left a gap in the middle would produce two events
    interleaved in time, which nothing downstream expects.
    """
    if not overrides.merge_groups:
        return events

    for group in overrides.merge_groups:
        indices = sorted(
            index
            for index, event in enumerate(events)
            if any(member.hash in group for member in event.members)
        )
        if len(indices) < 2:
            continue
        span = events[indices[0] : indices[-1] + 1]
        if len({event.local_date for event in span}) > 1:
            logger.warning("overrides: skipping a merge that would join events across days")
            continue
        merged = DetectedEvent(
            span[0].local_date,
            span[0].seq,
            [member for event in span for member in event.members],
        )
        events = events[: indices[0]] + [merged] + events[indices[-1] + 1 :]

    by_day: dict[str, int] = {}
    for event in events:
        by_day[event.local_date] = by_day.get(event.local_date, 0) + 1
        event.seq = by_day[event.local_date]
    return events


class EventStage(WholeTripStage):
    """Group each day's media into events."""

    name = "events"
    version = 1
    # Aggregate over the whole media set: a cached result goes stale the moment `scan` adds a
    # photo, leaving it in no event -- and since everything downstream is scoped by event, an
    # item in no event is invisible to dedup, selection, landmarks and the timeline at once.
    always_run = True

    def run(self, ctx: StageContext) -> None:
        media_list = list(iter_media(ctx.conn))
        undated = sum(1 for m in media_list if not m.taken_local or not m.taken_utc)
        if undated:
            logger.warning(
                "events: %d item(s) have no usable timestamp and belong to no event", undated
            )

        overrides = resolve(ctx.overrides, ctx.conn)
        events = detect_events(media_list, ctx.config, overrides)
        day_ids = _day_ids(ctx.conn)
        missing_days = {e.local_date for e in events} - set(day_ids)
        if missing_days:
            logger.warning(
                "events: no day row for %s -- run the days stage first",
                ", ".join(sorted(missing_days)),
            )

        _replace_events(ctx.conn, events, day_ids, overrides)
        logger.info("events: %d event(s) across %d day(s)", len(events), len(day_ids))


def _day_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["local_date"]: row["id"]
        for row in conn.execute("SELECT id, local_date FROM day WHERE trip_id = 1")
    }


def _replace_events(
    conn: sqlite3.Connection,
    events: list[DetectedEvent],
    day_ids: dict[str, int],
    overrides: ResolvedOverrides | None = None,
) -> None:
    """Rewrite the event rows for this trip.

    Deleting and re-inserting rather than reconciling in place: event identity is its position in
    a day's sequence, which is derived entirely from the media set, so there is nothing stable to
    reconcile against. `media_event` cascades on delete. Anything that later hangs off an event id
    (clusters) is rebuilt by its own always-run stage for the same reason.
    """
    labels = (overrides or ResolvedOverrides()).event_labels
    conn.execute(
        "DELETE FROM event WHERE day_id IN (SELECT id FROM day WHERE trip_id = 1)",
    )
    for event in events:
        day_id = day_ids.get(event.local_date)
        if day_id is None:
            continue
        centroid_lat, centroid_lon = event.centroid
        label = next(
            (labels[member.hash] for member in event.members if member.hash in labels), None
        )
        cursor = conn.execute(
            """
            INSERT INTO event (day_id, seq, start_utc, end_utc, centroid_lat, centroid_lon,
                               place_id, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day_id,
                event.seq,
                event.start_utc,
                event.end_utc,
                centroid_lat,
                centroid_lon,
                event.place_id,
                label,
            ),
        )
        event_id = cursor.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO media_event (media_hash, event_id) VALUES (?, ?)",
            [(media.hash, event_id) for media in event.members],
        )
