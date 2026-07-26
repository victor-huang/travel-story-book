"""Module 3: GPS backfill -- fill missing locations from time-adjacent GPS-bearing items.

The point of this stage is device asymmetry: a phone tags every shot with GPS, a standalone
camera never does. Rather than lose those items from the map, we interpolate their location from
whichever items -- **on any device** -- were closest in time and did carry a real GPS fix. P01's
real-data profile (`dev_plan/p01_profile_findings.md`) found this is a small correction (6 of
286 items, 2%) and an easy one: the median gap between consecutive shots across the whole library
is 1 minute, so a usable neighbor is almost always close by.

## Ordering

Everything here orders and measures gaps by `taken_utc`, never `taken_local` -- two devices in
different timezones (or one device with a wrong clock) interleave correctly only in UTC. See
`pipeline/timezones.py`'s module docstring for why this matters; that stage is what populates
`taken_utc` before this one runs.

## What counts as an anchor

Only items whose `gps_source` is `GpsSource.EXIF` or `GpsSource.MANUAL` are used as interpolation
anchors -- i.e. *measured* or *user-asserted* coordinates, never `GpsSource.INTERPOLATED` ones.
A previously-interpolated point is already a guess; chaining a new guess off an old one would
compound error silently and make a rerun's output depend on iteration order. This also keeps the
stage trivially idempotent: on a second run every item this stage filled already has
`gps_source == INTERPOLATED` and is therefore skipped (rule below), while the ground-truth
anchors are unchanged, so the same inputs produce the same outputs.

## Never overwrite ground truth

Only items with `gps_source == GpsSource.NONE` are candidates. An `EXIF` or `MANUAL` location is
ground truth and is never touched, even if a "better" interpolation would nominally exist.

## The window refusal

An item is only filled if it has a usable anchor -- one no more than
`config.time.gps_interpolation_window_minutes` away -- on the near side being used. Guessing a
location across a four-hour gap is fiction, not data, so items beyond the window on both sides
are left exactly as they came in: `gps_source` stays `NONE`, `lat`/`lon` stay `None`. That
explicit negative is the point -- "could not determine" must never look like "determined
imprecisely" (the same discipline `quality.py` applies to `face_count`).

## One-sided neighbors: decision and why

An item can have a usable anchor on only one side (start/end of the trip, or a gap on the other
side wider than the window). We *do* extrapolate from a single neighbor, using that neighbor's
coordinates directly (no velocity model -- there is only one data point), but at a fixed
confidence penalty (`_ONE_SIDED_CONFIDENCE_PENALTY = 0.7`) relative to two-sided interpolation at
a comparable distance. Reasoning: a neighbor a few minutes away is decent evidence of "still at
the same place" (real gaps are usually short per P01), but the one-sided case gives no
information about *motion* the way two bracketing fixes do, so it deserves to read as
less certain even when close, and the window bound already keeps a *distant* single neighbor
from being used as if it were nearby.

## Confidence formula

Both branches degrade with time distance to the anchor(s) used, scaled by the configured window:

* **Two-sided** (anchors before and after, both within the window): confidence is
  `max(0, 1 - (gap_before + gap_after) / (2 * window))`. The total span between the two anchors
  bounds how far the subject could plausibly have moved along an assumed straight path between
  them, regardless of where the target instant sits within that span.
* **One-sided** (anchor on only one side): confidence is
  `max(0, 1 - gap / window) * _ONE_SIDED_CONFIDENCE_PENALTY`.

Both are pure functions of gap and window, so confidence is strictly non-increasing in time
distance and directly comparable across items -- see `TestConfidenceDecreasesWithDistance` in
the unit tests.

## Config

Uses the existing `config.time.gps_interpolation_window_minutes` (already present in
`config.py` for T12's own gap checks) -- no new config field was needed. The one-sided penalty
constant is a stage-internal heuristic, not a user-facing threshold, so it is a module constant
rather than a config field (same call `timezones.py` makes for its clock-offset heuristics).

## `always_run`

Set `True`. This stage is an aggregate over the whole media set: its cache key is the constant
`TRIP_SENTINEL`, so without `always_run` a second `scan` that discovers a new GPS-less photo
would never trigger a recompute, and that new item would keep `gps_source = NONE` forever --
silently dropping out of the map. This is exactly the bug the integration notes describe for
`timezones`. The work itself is cheap, pure, in-memory list processing, so running it on every
build is not costly.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

from story_book.config import Config
from story_book.db.connection import iter_media, upsert_media
from story_book.db.models import GpsSource, Media
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)

_ONE_SIDED_CONFIDENCE_PENALTY = 0.7
"""Discount applied to extrapolation from a single neighbor -- see module docstring."""


@dataclass(slots=True, frozen=True)
class _Anchor:
    """A ground-truth (measured or manual) fix this stage may interpolate from."""

    utc: datetime
    lat: float
    lon: float


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _build_anchors(media_list: list[Media]) -> list[_Anchor]:
    """Ground-truth GPS fixes, sorted by capture instant. Excludes anything already
    interpolated -- see "What counts as an anchor" in the module docstring."""
    anchors = [
        _Anchor(_parse_utc(media.taken_utc), media.lat, media.lon)
        for media in media_list
        if media.taken_utc is not None
        and media.has_gps
        and media.gps_source in (GpsSource.EXIF, GpsSource.MANUAL)
    ]
    anchors.sort(key=lambda anchor: anchor.utc)
    return anchors


def _confidence_two_sided(gap_before: float, gap_after: float, window_minutes: float) -> float:
    span = gap_before + gap_after
    return max(0.0, 1.0 - span / (2 * window_minutes))


def _confidence_one_sided(gap: float, window_minutes: float) -> float:
    return max(0.0, 1.0 - gap / window_minutes) * _ONE_SIDED_CONFIDENCE_PENALTY


def backfill_gps(media_list: list[Media], config: Config) -> list[Media]:
    """Pure interpolation over an in-memory list of media. No DB, no filesystem.

    Mutates and returns the subset of `media_list` that got a new interpolated location.
    Everything else -- items that already have GPS, and GPS-less items with no usable
    neighbor -- is left completely untouched (not just unmoved: also not returned), so a
    caller can tell exactly what changed.
    """
    window_minutes = config.time.gps_interpolation_window_minutes
    anchors = _build_anchors(media_list)
    anchor_utcs = [anchor.utc for anchor in anchors]

    filled: list[Media] = []
    for media in media_list:
        if media.gps_source != GpsSource.NONE:
            continue
        if media.taken_utc is None:
            continue  # no time to interpolate from -- stays NONE, not a guess

        target = _parse_utc(media.taken_utc)
        idx = bisect_left(anchor_utcs, target)
        before = anchors[idx - 1] if idx > 0 else None
        after = anchors[idx] if idx < len(anchors) else None

        gap_before = (target - before.utc).total_seconds() / 60 if before else None
        gap_after = (after.utc - target).total_seconds() / 60 if after else None

        if gap_before is not None and gap_before > window_minutes:
            before, gap_before = None, None
        if gap_after is not None and gap_after > window_minutes:
            after, gap_after = None, None

        if before is None and after is None:
            continue  # no neighbor within the window on either side -- stays NONE

        if before is not None and after is not None:
            span_seconds = (after.utc - before.utc).total_seconds()
            frac = (target - before.utc).total_seconds() / span_seconds if span_seconds else 0.0
            lat = before.lat + frac * (after.lat - before.lat)
            lon = before.lon + frac * (after.lon - before.lon)
            confidence = _confidence_two_sided(gap_before, gap_after, window_minutes)
        else:
            anchor = before if before is not None else after
            gap = gap_before if before is not None else gap_after
            lat, lon = anchor.lat, anchor.lon
            confidence = _confidence_one_sided(gap, window_minutes)

        media.lat = lat
        media.lon = lon
        media.gps_source = GpsSource.INTERPOLATED
        media.gps_confidence = round(confidence, 4)
        filled.append(media)

    return filled


class GpsBackfillStage(WholeTripStage):
    """Fill `lat`/`lon` for GPS-less items from time-adjacent GPS-bearing items on any device.
    See the module docstring for anchor selection, the window refusal, one-sided handling, and
    the confidence formula."""

    name = "gps_backfill"
    version = 1
    # Aggregate over the whole media set -- see "always_run" in the module docstring.
    always_run = True
    description = "Interpolate missing GPS coordinates from time-adjacent GPS-bearing items."

    def run(self, ctx: StageContext) -> None:
        media_list = list(iter_media(ctx.conn))
        filled = backfill_gps(media_list, ctx.config)
        for media in filled:
            upsert_media(ctx.conn, media)
        if filled:
            logger.info("gps_backfill: interpolated %d item(s)", len(filled))
