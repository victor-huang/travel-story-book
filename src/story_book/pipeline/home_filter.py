"""Home-location privacy filter (T26).

The plan names the exact failure mode this stage exists to prevent: leaking the user's home
address into a shared album by including a photo taken in their kitchen. The whole exported
package is handed to a third-party AI service, so this is a privacy guarantee, not a feature --
getting it wrong in the "included" direction is the failure that matters.

This stage does one thing: for every `media` row with coordinates, compute the great-circle
(haversine) distance to `config.home` and set `media.is_near_home` when that distance is within
`config.home.exclusion_km`. It never touches anything else.

**Fail toward privacy.** Two decisions follow directly from the plan's privacy-by-default
constraint:

* **No coordinates -> `is_near_home` stays `False`, but exports must not treat that as "safe".**
  A media item with no lat/lon cannot be distance-tested, so `is_near_home` -- which claims a
  *measured* proximity -- would be a lie if set `True` for it. Instead, the row stays honest and
  the export-facing predicate `should_exclude_from_export` treats "no coordinates" as
  export-unsafe on its own, independent of the flag. That keeps `is_near_home` meaning exactly
  what it says (useful for diagnostics/UI: "why was this excluded") while still failing toward
  privacy at the one place that actually matters -- what an export includes.

* **Interpolated coordinates (`gps_source == INTERPOLATED`) are tested exactly like measured
  ones.** An interpolated point is, by construction, borrowed from nearby GPS-bearing neighbors
  in time -- for a media item actually taken at home, that estimate is likely to land near home
  too. Excluding interpolated points from the test would open exactly the blind spot this stage
  exists to close: a GPS-less home photo, backfilled by T20, silently skipping the filter that
  was supposed to catch it. So interpolation is not a reason to skip the test; a low-confidence
  interpolated point that happens to be far from home is not flagged, same as any other item
  outside the radius -- confidence is T20's concern, not this stage's.

Boundary: the radius test is **inclusive** (`distance_km <= exclusion_km` is flagged), the
ambiguous case resolved the same direction as everything else here.

Downstream contract: call `should_exclude_from_export(media)`, not `media.is_near_home`
directly, wherever an export decides what to include.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from math import asin, cos, radians, sin, sqrt

from story_book.config import HomeLocation
from story_book.db.connection import iter_media, upsert_media
from story_book.db.models import Media
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088
"""Mean Earth radius (IUGG authalic mean), the standard constant for haversine distance."""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers.

    Euclidean distance on raw degrees is wrong by a factor that shrinks with latitude (a degree
    of longitude is ~111 km at the equator but ~78 km at 45 deg and ~0 km at the poles), so this
    stage cannot use it -- the haversine formula accounts for the sphere.
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def is_within_home_radius(media: Media, home: HomeLocation) -> bool:
    """Distance test against the configured home. `False` (never `True`) when `media` has no
    coordinates -- there is nothing to measure, see module docstring for how exports must treat
    that case instead.
    """
    if media.lat is None or media.lon is None:
        return False
    return haversine_km(media.lat, media.lon, home.lat, home.lon) <= home.exclusion_km


def should_exclude_from_export(media: Media, home: HomeLocation | None) -> bool:
    """The predicate every export must call before including an item.

    Two cases exclude:

    * The item is a match against home, measured or interpolated.
    * Its location is unknown **and a home is configured** -- fail-toward-privacy, since an item
      never checked is not the same as one checked and cleared.

    The `home` argument is what makes the second case correct. Without it the predicate excluded
    every coordinate-less item unconditionally, which protects nothing when no home is set and
    quietly deletes real content: the plan's own input list includes a Sony camera and a GoPro,
    neither of which records GPS, so a DSLR-heavy trip whose gaps exceed the interpolation window
    would have lost those photos from the book with no error. Guarding a home location that was
    never configured is not caution, just data loss.

    When a home *is* configured, unknown-location items are genuinely ambiguous and are excluded --
    but callers should report the count (see `unknown_location_count`) so the user can widen the
    interpolation window or override, rather than silently losing photos.
    """
    if media.is_near_home:
        return True
    if home is None:
        return False
    return media.lat is None or media.lon is None


def unknown_location_count(media_items: Iterable[Media]) -> int:
    """How many items an export would drop for want of a location. Report this, never hide it."""
    return sum(1 for media in media_items if media.lat is None or media.lon is None)


class HomeFilterStage(WholeTripStage):
    """Flag every media item within `config.home.exclusion_km` of the configured home."""

    name = "home_filter"
    version = 1
    # This is an aggregate privacy guarantee over the whole media set, not a per-item fact cached
    # once. A cached "ok" would mean a photo scan added after the first run keeps is_near_home
    # unset -- an unflagged photo near home slipping into an export is the worst instance of the
    # exact bug class (`timezones`) always_run was introduced to close. Re-testing every item
    # against a stored lat/lon is cheap and purely idempotent, so there is no cost to paying it
    # every run.
    always_run = True
    description = "Flag media within the configured home radius so exports exclude it by default."

    def run(self, ctx: StageContext) -> None:
        home = ctx.config.home
        if home is None:
            logger.warning(
                "home_filter: no `home` configured -- the privacy filter did NOT run. No media "
                "was checked, and none is flagged as near home. This is a gap, not a clean "
                "result: configure `config.home` (lat, lon, exclusion_km) to enable this "
                "guarantee."
            )
            return

        media_list = list(iter_media(ctx.conn))
        flagged = 0
        changed = 0
        untested = 0

        for media in media_list:
            if media.lat is None or media.lon is None:
                untested += 1
                continue

            near = is_within_home_radius(media, home)
            if near:
                flagged += 1
            if near != media.is_near_home:
                media.is_near_home = near
                upsert_media(ctx.conn, media)
                changed += 1

        logger.info(
            "home_filter: checked %d media item(s) against home (%.4f, %.4f) within %.1f km -- "
            "%d flagged as near home (%d changed from the previous run).",
            len(media_list) - untested,
            home.lat,
            home.lon,
            home.exclusion_km,
            flagged,
            changed,
        )
        if untested:
            logger.warning(
                "home_filter: %d media item(s) have no coordinates and could not be "
                "distance-tested. They are NOT flagged is_near_home, but "
                "should_exclude_from_export treats them as export-unsafe by default -- see the "
                "module docstring.",
                untested,
            )
