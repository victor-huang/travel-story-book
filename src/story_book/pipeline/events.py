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

**A maximum duration is what actually breaks up a long day.** P02 found a real day where 129
items spanning 11:31-20:15 collapsed into a single "event". The first diagnosis blamed the
centroid rule -- the theory being that a whole-event centroid converges on the average of
everything so far, so movement stops registering -- and proposed comparing against only the most
recent items instead.

**That theory was tested and is wrong.** On the real day, a recent window of 6, 12, or 1000 items
produces identical results; the entire 4-to-7 event improvement came from the duration backstop.
Worse, on synthetic gradual drift the recent window is actively *worse*: it follows you, so you
are never far from it and it never splits, while a whole-event centroid lags behind the drift and
does eventually notice. So this module keeps the simple whole-event centroid and adds
`events.max_minutes`.

The honest description of the remaining gap: wandering a city centre for nine hours triggers
neither rule, because shots are minutes apart and every position sits within `jump_km` of a
central point. Only the clock catches it. Whether `jump_km` itself should be smaller is a tuning
question that needs the P03 labelled set -- guessing at it here is what this project keeps
learning not to do.
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

    if _minutes_between(current[0], candidate) > events.max_minutes:
        return True, "max_duration"

    return False, ""


def detect_events(media_list: list[Media], config: Config) -> list[DetectedEvent]:
    """Split each day's media into events. Pure: no DB, no filesystem.

    Undated items are excluded -- they cannot be placed in a chronology, and inventing a position
    for them would put a photo in a story it may not belong to. The caller counts them.
    """
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

        events = detect_events(media_list, ctx.config)
        day_ids = _day_ids(ctx.conn)
        missing_days = {e.local_date for e in events} - set(day_ids)
        if missing_days:
            logger.warning(
                "events: no day row for %s -- run the days stage first",
                ", ".join(sorted(missing_days)),
            )

        _replace_events(ctx.conn, events, day_ids)
        logger.info("events: %d event(s) across %d day(s)", len(events), len(day_ids))


def _day_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["local_date"]: row["id"]
        for row in conn.execute("SELECT id, local_date FROM day WHERE trip_id = 1")
    }


def _replace_events(
    conn: sqlite3.Connection, events: list[DetectedEvent], day_ids: dict[str, int]
) -> None:
    """Rewrite the event rows for this trip.

    Deleting and re-inserting rather than reconciling in place: event identity is its position in
    a day's sequence, which is derived entirely from the media set, so there is nothing stable to
    reconcile against. `media_event` cascades on delete. Anything that later hangs off an event id
    (clusters) is rebuilt by its own always-run stage for the same reason.
    """
    conn.execute(
        "DELETE FROM event WHERE day_id IN (SELECT id FROM day WHERE trip_id = 1)",
    )
    for event in events:
        day_id = day_ids.get(event.local_date)
        if day_id is None:
            continue
        centroid_lat, centroid_lon = event.centroid
        cursor = conn.execute(
            """
            INSERT INTO event (day_id, seq, start_utc, end_utc, centroid_lat, centroid_lon,
                               place_id, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                day_id,
                event.seq,
                event.start_utc,
                event.end_utc,
                centroid_lat,
                centroid_lon,
                event.place_id,
            ),
        )
        event_id = cursor.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO media_event (media_hash, event_id) VALUES (?, ?)",
            [(media.hash, event_id) for media in event.members],
        )
