"""Module 10: selection.

Decides which photos represent the trip. Three scopes, each answering a different question:

* **cluster** -- of these near-duplicates, which one do we keep? The rest are never deleted, just
  not chosen.
* **event** -- which few photos represent this internal cluster? Used to sample landmark
  recognition cheaply, not shown to a reader.
* **day** and **trip** -- the highlights that actually reach the book.

Three findings from real use shape the day-level rule, and none of them are obvious:

**Allocate by photo count, not equally.** Density was first proposed as a way to *segment* events
("hotspots 500 m apart are different events"), and measured against hand labels it fails badly --
hotspot distances overlap almost entirely between same-event and different-event pairs, and places
get revisited hours apart. But the underlying instinct is right and lands here instead: on one real
day, seven ~100 m cells held above-average photo counts, topping out at 28 photos in one cell, and
those are exactly the places that mattered. **Taking many photos somewhere is evidence of
importance, not of a boundary.**

**Spread across the day.** An earlier prototype returned five highlights all shot within fifteen
minutes to represent nine hours, because it filled its quota from whichever cluster scored highest.
Allocation is therefore per event, so a long afternoon cannot be represented by one moment.

**Square-root the weighting.** Straight proportional allocation lets a single 129-photo cluster
swallow an entire day's quota. `sqrt` keeps the ordering -- more photos still earns more slots --
while leaving room for the quiet parts of a day that a book still needs.

Duplicates never compete with each other: only a cluster's keeper is eligible, so five shots of one
façade cannot occupy five highlight slots.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from story_book.config import Config
from story_book.db.models import Media, SelectionScope
from story_book.pipeline.base import StageContext, WholeTripStage
from story_book.pipeline.home_filter import should_exclude_from_export

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Candidate:
    media: Media
    overall: float
    content_class: str | None
    cluster_id: int | None
    event_id: int
    day_id: int
    vector: list[float] | None

    @property
    def pixels(self) -> int:
        return (self.media.width or 0) * (self.media.height or 0)


def keeper_sort_key(candidate: Candidate) -> tuple:
    """Best first: quality, then resolution, then earliest, then hash.

    The trailing hash is not decoration -- without a total order, two equally-scored photos would
    swap keeper between runs and the output would be unstable for no reason.
    """
    return (
        -candidate.overall,
        -candidate.pixels,
        candidate.media.taken_utc or "",
        candidate.media.hash,
    )


def is_eligible(candidate: Candidate, config: Config) -> bool:
    """Whether a photo may appear in the book at all."""
    if should_exclude_from_export(candidate.media, config.home):
        return False
    if candidate.content_class in config.quality.reject_content_classes:
        return False
    return candidate.overall >= config.quality.min_overall_for_highlight


def allocate(counts: dict[int, int], quota: int) -> dict[int, int]:
    """Split `quota` slots across groups, weighted by the square root of each group's size.

    Square root rather than linear: on a real day one cluster held 129 of 141 photos, and straight
    proportional allocation would have given the rest of the day a single slot between them. Every
    group with any candidate gets at least one, because a place worth photographing at all is worth
    one frame in the book.
    """
    if not counts or quota <= 0:
        return {}
    groups = sorted(counts)
    if len(groups) >= quota:
        # More groups than slots: give one each to the largest, rather than fractions to all.
        ranked = sorted(groups, key=lambda g: (-counts[g], g))
        return {group: 1 for group in ranked[:quota]}

    weights = {group: math.sqrt(counts[group]) for group in groups}
    total = sum(weights.values())
    allocation = {group: 1 for group in groups}
    remaining = quota - len(groups)

    # Largest-remainder apportionment, so the leftovers go where the fractions were biggest rather
    # than to whichever group happens to sort first.
    shares = {g: remaining * weights[g] / total for g in groups}
    for group in groups:
        allocation[group] += int(shares[group])
    leftover = quota - sum(allocation.values())
    for group in sorted(groups, key=lambda g: (-(shares[g] % 1), g))[:leftover]:
        allocation[group] += 1
    return allocation


def pick_diverse(candidates: list[Candidate], limit: int, min_distance: float) -> list[Candidate]:
    """Best-quality first, skipping anything too close to an already-chosen photo.

    Quality alone returns near-identical frames: the sharpest five shots of one façade all score
    within a hair of each other. Deduplication removes only *near*-duplicates; two genuinely
    different photos of the same building survive it and still make a dull page.
    """
    chosen: list[Candidate] = []
    for candidate in sorted(candidates, key=keeper_sort_key):
        if len(chosen) >= limit:
            break
        if candidate.vector is None:
            chosen.append(candidate)
            continue
        closest = max(
            (_cosine(candidate.vector, other.vector) for other in chosen if other.vector),
            default=0.0,
        )
        if 1.0 - closest >= min_distance or not chosen:
            chosen.append(candidate)
    return chosen


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


class SelectionStage(WholeTripStage):
    """Choose cluster keepers, event representatives, and day/trip highlights."""

    name = "selection"
    version = 1
    # Everything here is derived from scores, clusters and events, all of which are themselves
    # rebuilt on every run. A cached selection would silently describe an older library.
    always_run = True

    def run(self, ctx: StageContext) -> None:
        candidates = _load(ctx.conn)
        if not candidates:
            logger.info("selection: nothing scored yet")
            return

        _clear(ctx.conn)
        keepers = self._choose_keepers(ctx, candidates)
        eligible = [
            c
            for c in candidates
            if is_eligible(c, ctx.config)
            and (c.cluster_id is None or keepers.get(c.cluster_id) == c.media.hash)
        ]
        excluded = len(candidates) - len(eligible)

        self._choose_event_representatives(ctx, eligible)
        day_picks = self._choose_day_highlights(ctx, eligible)
        self._choose_trip_highlights(ctx, day_picks)

        logger.info(
            "selection: %d keeper(s), %d day highlight(s); %d candidate(s) not eligible",
            len(keepers),
            len(day_picks),
            excluded,
        )

    def _choose_keepers(self, ctx: StageContext, candidates: list[Candidate]) -> dict[int, str]:
        by_cluster: dict[int, list[Candidate]] = {}
        for candidate in candidates:
            if candidate.cluster_id is not None:
                by_cluster.setdefault(candidate.cluster_id, []).append(candidate)

        keepers: dict[int, str] = {}
        for cluster_id, members in by_cluster.items():
            best = min(members, key=keeper_sort_key)
            keepers[cluster_id] = best.media.hash
            ctx.conn.execute(
                "UPDATE cluster SET keeper_hash = ? WHERE id = ?", (best.media.hash, cluster_id)
            )
            _record(ctx.conn, best, SelectionScope.CLUSTER, cluster_id, 1, "best in cluster")
        return keepers

    def _choose_event_representatives(self, ctx: StageContext, eligible: list[Candidate]) -> None:
        """A few per internal cluster, used to sample landmark recognition cheaply."""
        by_event: dict[int, list[Candidate]] = {}
        for candidate in eligible:
            by_event.setdefault(candidate.event_id, []).append(candidate)

        limit = ctx.config.selection.highlights_per_event
        distance = ctx.config.selection.diversity_min_distance
        for event_id, members in by_event.items():
            for rank, candidate in enumerate(pick_diverse(members, limit, distance), start=1):
                _record(ctx.conn, candidate, SelectionScope.EVENT, event_id, rank, "event sample")

    def _choose_day_highlights(
        self, ctx: StageContext, eligible: list[Candidate]
    ) -> list[Candidate]:
        by_day: dict[int, list[Candidate]] = {}
        for candidate in eligible:
            by_day.setdefault(candidate.day_id, []).append(candidate)

        quota = ctx.config.selection.highlights_per_day
        distance = ctx.config.selection.diversity_min_distance
        picked: list[Candidate] = []

        for day_id, members in by_day.items():
            by_event: dict[int, list[Candidate]] = {}
            for candidate in members:
                by_event.setdefault(candidate.event_id, []).append(candidate)

            allocation = allocate({e: len(m) for e, m in by_event.items()}, quota)
            chosen: list[Candidate] = []
            for event_id, slots in allocation.items():
                chosen.extend(pick_diverse(by_event[event_id], slots, distance))

            chosen.sort(key=lambda c: c.media.taken_utc or "")
            for rank, candidate in enumerate(chosen, start=1):
                _record(ctx.conn, candidate, SelectionScope.DAY, day_id, rank, "day highlight")
            picked.extend(chosen)
        return picked

    def _choose_trip_highlights(self, ctx: StageContext, day_picks: list[Candidate]) -> None:
        quota = ctx.config.selection.highlights_per_day
        distance = ctx.config.selection.diversity_min_distance
        by_day: dict[int, list[Candidate]] = {}
        for candidate in day_picks:
            by_day.setdefault(candidate.day_id, []).append(candidate)

        allocation = allocate({d: len(m) for d, m in by_day.items()}, quota)
        chosen: list[Candidate] = []
        for day_id, slots in allocation.items():
            chosen.extend(pick_diverse(by_day[day_id], slots, distance))

        chosen.sort(key=lambda c: c.media.taken_utc or "")
        for rank, candidate in enumerate(chosen, start=1):
            _record(ctx.conn, candidate, SelectionScope.TRIP, 1, rank, "trip highlight")


def _load(conn: sqlite3.Connection) -> list[Candidate]:
    vectors = _vectors(conn)
    out: list[Candidate] = []
    for row in conn.execute(
        """
        SELECT m.*, s.overall, s.content_class, mc.cluster_id, me.event_id, e.day_id
        FROM media m
        JOIN score s ON s.media_hash = m.hash
        JOIN media_event me ON me.media_hash = m.hash
        JOIN event e ON e.id = me.event_id
        LEFT JOIN media_cluster mc ON mc.media_hash = m.hash
        WHERE m.kind = 'image' AND s.overall IS NOT NULL
        """
    ):
        media = Media.from_row(row)
        out.append(
            Candidate(
                media=media,
                overall=row["overall"],
                content_class=row["content_class"],
                cluster_id=row["cluster_id"],
                event_id=row["event_id"],
                day_id=row["day_id"],
                vector=vectors.get(media.hash),
            )
        )
    return out


def _vectors(conn: sqlite3.Connection) -> dict[str, list[float]]:
    try:
        from story_book.pipeline.embeddings import decode_vector
    except ImportError:  # pragma: no cover - the clip extra is optional
        return {}
    return {
        row["media_hash"]: decode_vector(row["vector"])
        for row in conn.execute("SELECT media_hash, vector FROM embedding")
    }


def _clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM selection")
    conn.execute("UPDATE cluster SET keeper_hash = NULL")


def _record(
    conn: sqlite3.Connection,
    candidate: Candidate,
    scope: SelectionScope,
    scope_id: int,
    rank: int,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO selection (media_hash, scope, scope_id, rank, reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (media_hash, scope, scope_id) DO UPDATE SET
            rank = excluded.rank, reason = excluded.reason
        """,
        (candidate.media.hash, str(scope), scope_id, rank, reason),
    )


def selection_report(conn: sqlite3.Connection, scope: SelectionScope) -> list[dict[str, Any]]:
    """Selected media for human review. Used by the integrator, not the pipeline."""
    return [
        {
            "scope_id": row["scope_id"],
            "rank": row["rank"],
            "name": row["path"].rsplit("/", 1)[-1],
            "taken": row["taken_local"],
            "overall": row["overall"],
            "content_class": row["content_class"],
            "path": row["path"],
        }
        for row in conn.execute(
            """
            SELECT s.scope_id, s.rank, m.path, m.taken_local, sc.overall, sc.content_class
            FROM selection s
            JOIN media m ON m.hash = s.media_hash
            LEFT JOIN score sc ON sc.media_hash = m.hash
            WHERE s.scope = ?
            ORDER BY s.scope_id, s.rank
            """,
            (str(scope),),
        )
    ]
