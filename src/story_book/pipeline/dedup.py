"""Module 7: near-duplicate clustering.

Two mechanisms for two genuinely different problems, which the plan's first draft conflated:

* **pHash at a tight Hamming threshold** catches near-exact duplicates -- the same photo imported
  twice, a Photos export overlapping a camera import, burst frames a fraction of a second apart.
* **CLIP cosine at a looser threshold** catches *semantically* similar shots -- eleven photos of
  the same façade from slightly different angles, which are not bit-similar at all.

Clustering runs **within an event only**. Events are internal scoping (see `events.py`), and this
is the reason they exist: comparing every photo against every other photo is both quadratic and
wrong, since two identical-looking cathedral shots taken on different days are not duplicates.
That also sets the one hard constraint on how coarse events may be -- **a near-duplicate pair
split across two events can never be found**, which is why event detection is tuned to
under-split rather than over-split.

Nothing is ever deleted. A cluster is metadata; `selection` (T30) later marks one member as the
keeper and the rest simply do not appear in the book unless `--include-all` is passed.

The plan is explicit that **a false merge is worse than a missed duplicate**: losing a distinct
photo from the book is a real loss, while an extra near-duplicate is a minor annoyance. Both
thresholds are therefore set to be conservative, and the eval reports pairwise precision and
recall separately so the two failure modes stay visible.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from story_book.config import Config
from story_book.db.connection import iter_media
from story_book.db.models import ClusterKind, Media, MediaKind
from story_book.pipeline.base import (
    Executor,
    PerItemStage,
    SkipItem,
    StageContext,
    WholeTripStage,
)

logger = logging.getLogger(__name__)

PHASH_SIZE = 32
"""Working edge for the DCT. 32x32 downsample keeping the top-left 8x8 coefficients is the
standard construction: it discards high-frequency detail so that resizing, re-compression and
mild edits leave the hash unchanged."""

PHASH_BITS = 8

PHASH_WIDTH = PHASH_BITS * PHASH_BITS - 1
"""63 bits: the 8x8 low-frequency block minus the DC term. See `phash`."""


def phash(path: str) -> int:
    """64-bit perceptual hash.

    DCT of a 32x32 greyscale, low-frequency 8x8 block, thresholded at its median.

    Implemented directly rather than pulled in as a dependency -- it is a dozen lines of numpy and
    avoids another package for something this stable. Loading goes through Pillow so HEIC works.
    """
    with Image.open(path) as handle:
        grey = handle.convert("L").resize((PHASH_SIZE, PHASH_SIZE), Image.Resampling.LANCZOS)
    pixels = np.asarray(grey, dtype=np.float64)

    coefficients = _dct2(pixels)[:PHASH_BITS, :PHASH_BITS]
    # Drop the DC term. It encodes overall brightness rather than structure, and being far larger
    # than every AC coefficient it sits above the median essentially always -- a bit that is
    # constant across the whole library carries no information. Dropping it also leaves 63 bits,
    # which matters practically: SQLite's INTEGER is *signed* 64-bit, so a full 64-bit hash
    # overflows on write for every image whose top bit happens to be set.
    without_dc = coefficients.flatten()[1:]
    median = np.median(without_dc)

    bits = 0
    for index, value in enumerate(without_dc):
        if value > median:
            bits |= 1 << index
    return bits


def _dct2(matrix: np.ndarray) -> np.ndarray:
    """2-D DCT-II via matrix multiplication. Small and fixed-size, so this is fast enough."""
    size = matrix.shape[0]
    indices = np.arange(size)
    basis = np.cos(np.pi * (2 * indices[:, None] + 1) * indices[None, :] / (2 * size))
    basis[:, 0] *= 1 / np.sqrt(2)
    return basis.T @ matrix @ basis


def hamming(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


class PhashStage(PerItemStage):
    """Compute and cache a perceptual hash per image."""

    name = "phash"
    version = 1
    executor = Executor.PROCESS

    def select(self, ctx: StageContext) -> list[Media]:
        return list(iter_media(ctx.conn, kind=str(MediaKind.IMAGE)))

    def compute(self, media: Media, config: Config) -> int:
        if media.kind is not MediaKind.IMAGE:
            raise SkipItem("perceptual hashing applies to photos, not videos")
        try:
            return phash(media.path)
        except Exception as exc:
            raise ValueError(f"unreadable image: {media.path}") from exc

    def persist(self, ctx: StageContext, media: Media, payload: int) -> None:
        ctx.conn.execute(
            """
            INSERT INTO phash (media_hash, value) VALUES (?, ?)
            ON CONFLICT (media_hash) DO UPDATE SET value = excluded.value
            """,
            (media.hash, payload),
        )


@dataclass(slots=True)
class _Candidate:
    media: Media
    phash: int | None
    vector: np.ndarray | None
    taken: datetime | None


_KIND_RANK = {ClusterKind.EXACT: 0, ClusterKind.BURST: 1, ClusterKind.SIMILAR: 2}


def _complete_linkage(
    members: list[_Candidate], config: Config
) -> list[tuple[ClusterKind, list[_Candidate]]]:
    """Group members so that **every** pair inside a group is a match, not merely a chain of them.

    Single-linkage (union-find) was the first implementation, justified by "a burst drifts, so A~B
    and B~C should put A and C together". Real data destroyed that argument: with 141 photos in an
    event, chaining produced an 18-photo "burst" whose internal distances reached 40 bits -- eleven
    unrelated pairs merged for every real one. Transitivity is exactly what turns a per-pair false
    positive rate into a cluster-swallowing one.

    Complete linkage refuses to add a photo unless it matches everything already in the group, so a
    chain cannot bridge across the threshold. Greedy and order-dependent in principle; in practice
    members arrive in capture order, which is the order bursts occur in.
    """
    groups: list[tuple[list[_Candidate], list[ClusterKind]]] = []
    for candidate in members:
        for existing, kinds in groups:
            matches = [classify_pair(candidate, other, config) for other in existing]
            if all(kind is not None for kind in matches):
                existing.append(candidate)
                kinds.extend(k for k in matches if k is not None)
                break
        else:
            groups.append(([candidate], []))

    out: list[tuple[ClusterKind, list[_Candidate]]] = []
    for existing, kinds in groups:
        if len(existing) < 2:
            continue
        # The group takes the strongest relationship it contains: a burst that drifts into a
        # merely-similar frame is still fundamentally a burst.
        out.append((min(kinds, key=lambda k: _KIND_RANK[k]), existing))
    return out


def classify_pair(left: _Candidate, right: _Candidate, config: Config) -> ClusterKind | None:
    """How (or whether) two photos in the same event are duplicates of each other."""
    dedup = config.dedup

    if left.phash is not None and right.phash is not None:
        distance = hamming(left.phash, right.phash)
        if distance == 0:
            return ClusterKind.EXACT
        if distance <= dedup.phash_max_distance:
            seconds = _seconds_between(left, right)
            # A tight visual match *and* shot moments apart is a burst. The same match minutes
            # apart is a deliberate retake, which is still a duplicate but not a burst.
            if seconds is not None and seconds <= dedup.burst_max_seconds:
                return ClusterKind.BURST
            return ClusterKind.SIMILAR

    if left.vector is not None and right.vector is not None:
        cosine = float(np.dot(left.vector, right.vector))
        if cosine >= dedup.similar_min_cosine:
            return ClusterKind.SIMILAR

    return None


def _seconds_between(left: _Candidate, right: _Candidate) -> float | None:
    if left.taken is None or right.taken is None:
        return None
    return abs((right.taken - left.taken).total_seconds())


class DedupStage(WholeTripStage):
    """Group near-duplicates within each event."""

    name = "dedup"
    version = 1
    # Clusters are derived entirely from the media set and its events, so a cached result goes
    # stale as soon as either changes. Rebuilding is cheap next to the CLIP pass that feeds it.
    always_run = True

    def run(self, ctx: StageContext) -> None:
        candidates = self._load(ctx)
        if not candidates:
            logger.info("dedup: no events with photos yet")
            return

        _clear_clusters(ctx.conn)
        total_clusters = 0
        for event_id, members in candidates.items():
            for kind, group in self._cluster_event(members, ctx.config):
                if len(group) < 2:
                    continue
                _write_cluster(ctx.conn, event_id, kind, group)
                total_clusters += 1
        logger.info("dedup: %d cluster(s) across %d event(s)", total_clusters, len(candidates))

    def _cluster_event(
        self, members: list[_Candidate], config: Config
    ) -> list[tuple[ClusterKind, list[str]]]:
        return [
            (kind, sorted(c.media.hash for c in group))
            for kind, group in _complete_linkage(members, config)
        ]

    def _load(self, ctx: StageContext) -> dict[int, list[_Candidate]]:
        hashes = {
            row["media_hash"]: row["value"]
            for row in ctx.conn.execute("SELECT media_hash, value FROM phash")
        }
        vectors = _load_vectors(ctx.conn)

        by_event: dict[int, list[_Candidate]] = {}
        for row in ctx.conn.execute(
            """
            SELECT me.event_id, m.*
            FROM media_event me JOIN media m ON m.hash = me.media_hash
            WHERE m.kind = 'image'
            ORDER BY me.event_id, m.taken_utc
            """
        ):
            media = Media.from_row(row)
            by_event.setdefault(row["event_id"], []).append(
                _Candidate(
                    media=media,
                    phash=hashes.get(media.hash),
                    vector=vectors.get(media.hash),
                    taken=_parse(media.taken_utc),
                )
            )
        return by_event


def _load_vectors(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
    try:
        from story_book.pipeline.embeddings import decode_vector
    except ImportError:  # pragma: no cover - the clip extra may not be installed
        return {}
    return {
        row["media_hash"]: np.asarray(decode_vector(row["vector"]), dtype=np.float32)
        for row in conn.execute("SELECT media_hash, vector FROM embedding")
    }


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _clear_clusters(conn: sqlite3.Connection) -> None:
    """Rebuilt wholesale, like events: cluster identity is derived, so there is nothing stable to
    reconcile against. `media_cluster` cascades."""
    conn.execute(
        "DELETE FROM cluster WHERE event_id IN "
        "(SELECT id FROM event WHERE day_id IN (SELECT id FROM day WHERE trip_id = 1))"
    )


def _write_cluster(
    conn: sqlite3.Connection, event_id: int, kind: ClusterKind, members: list[str]
) -> None:
    cursor = conn.execute(
        "INSERT INTO cluster (event_id, kind, keeper_hash) VALUES (?, ?, NULL)",
        (event_id, str(kind)),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO media_cluster (media_hash, cluster_id) VALUES (?, ?)",
        [(member, cursor.lastrowid) for member in members],
    )


def cluster_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Clusters with their members, for human review. Used by the integrator, not the pipeline."""
    out: list[dict[str, Any]] = []
    for row in conn.execute("SELECT id, event_id, kind, keeper_hash FROM cluster ORDER BY id"):
        members = [
            {"hash": r["media_hash"], "name": Path(r["path"]).name, "taken": r["taken_local"]}
            for r in conn.execute(
                """
                SELECT m.hash AS media_hash, m.path, m.taken_local
                FROM media_cluster mc JOIN media m ON m.hash = mc.media_hash
                WHERE mc.cluster_id = ? ORDER BY m.taken_utc
                """,
                (row["id"],),
            )
        ]
        out.append({"id": row["id"], "kind": row["kind"], "members": members})
    return out
