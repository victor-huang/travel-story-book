"""Module 4: reverse geocoding.

Coordinates -> (place, city, region, country). Offline-first via the bundled
`reverse-geocoder` package, which ships its own GeoNames-derived city dataset and builds a
k-d tree at first use -- so the common case needs **no network and no rate limit**. Optional
Nominatim adds a POI-level name on top (a specific landmark rather than "the nearest city"),
aggressively cached in the `place` table keyed by coordinates rounded to
`config.geocode.coordinate_rounding_decimals` (4 decimal places is roughly 11m; the task's
~50m target is comfortably inside that), and rate-limited per Nominatim's usage policy via
`config.geocode.nominatim_min_interval_seconds`.

**Acceptance (Module 4 / T21):** "every event gets at least a city-level label with zero
network calls." Events don't exist yet (T24), so this module proves the equivalent: every
located `media` row resolves to a city-level `place` with zero network calls, and Nominatim is
never constructed or called unless `config.geocode.use_nominatim` is `True` -- see
`GeocodeStage.run` and the `TestNoNetworkByDefault` test classes in both test files.

Candidate places with distance, for T41 (the ChatGPT package) to ship "candidate places with
distances and confidences" per Module 14's P02 result (addition 3) instead of a single
unexplained guess: see `candidate_places()`, which needs no DB and no stage context.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from sqlite3 import Connection
from typing import Any, Protocol
from urllib.parse import urlencode

from story_book.config import GeocodeConfig
from story_book.db.connection import iter_media, upsert_media
from story_book.db.models import Place
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)

_EARTH_RADIUS_M = 6_371_000.0
DEFAULT_CANDIDATE_COUNT = 3
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def available() -> tuple[bool, str]:
    """Whether the offline geocoder dependency is installed.

    Checked by `GeocodeStage.available()` so a build without the `geo` extra still completes
    -- every stage must degrade rather than abort when a dependency is missing.
    """
    try:
        import reverse_geocoder  # noqa: F401
    except ImportError as exc:
        return False, f"reverse-geocoder is not installed (install the 'geo' extra): {exc}"
    return True, ""


class OfflineGeocoderLike(Protocol):
    """The two things of `reverse_geocoder.RGeocoder` this module relies on."""

    locations: list[dict[str, str]]
    tree: Any


_offline_geocoder: OfflineGeocoderLike | None = None


def get_offline_geocoder() -> OfflineGeocoderLike:
    """Real, offline `reverse_geocoder.RGeocoder`, built once per process.

    Building the k-d tree from the bundled dataset takes about a second and prints a "Loading
    formatted geocoded file..." banner, so callers must load it lazily and reuse the instance
    rather than constructing it per item. A separate function so tests can patch it without
    ever touching the real (installed but sizeable) dataset. `mode=1` is the single-threaded
    k-d tree -- this stage already runs once over the whole trip in the parent process, so the
    multi-process pool the default mode spins up would only add overhead.
    """
    global _offline_geocoder
    if _offline_geocoder is None:
        from reverse_geocoder import RGeocoder

        _offline_geocoder = RGeocoder(mode=1, verbose=False)
    return _offline_geocoder


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lon1_r, lat2_r, lon2_r = (radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def _query_indices(geocoder: OfflineGeocoderLike, lat: float, lon: float, count: int) -> list[int]:
    """Normalize `RGeocoder.tree.query`'s shape, which differs by `k`.

    scipy's `cKDTree.query` returns a flat `(1,)` array for `k=1` (each element a scalar index)
    and a `(1, k)` array for `k>1` (each element itself an array of indices). `raw[0]` is a
    non-iterable scalar in the first case and an iterable in the second, so `list(raw[0])`
    fails exactly when the flat form is in play.
    """
    _, raw = geocoder.tree.query([(lat, lon)], k=count)
    try:
        return list(raw[0])
    except TypeError:
        return list(raw)


@dataclass(slots=True)
class PlaceCandidate:
    """One offline gazetteer entry near a coordinate, with a real distance in metres.

    T41 (the ChatGPT package) ships a shortlist of these instead of a single guess, so the
    model has something it can verify against a photo rather than trust blindly -- Module 14's
    P02 result, addition 3 ("candidate places with distances and confidences").
    """

    name: str
    admin1: str | None
    admin2: str | None
    country_code: str | None
    lat: float
    lon: float
    distance_m: float


def candidate_places(
    lat: float,
    lon: float,
    count: int = DEFAULT_CANDIDATE_COUNT,
    *,
    geocoder: OfflineGeocoderLike | None = None,
) -> list[PlaceCandidate]:
    """The `count` nearest offline gazetteer entries to `(lat, lon)`, nearest first, each with
    a real great-circle distance in metres. Zero network, no DB, no `StageContext`.

    This is the public API T41 depends on: it takes a bare coordinate (e.g. an event centroid)
    and returns a verifiable shortlist rather than one unexplained guess. `geocoder` is
    injectable for tests; production callers omit it and share the lazily-built instance from
    `get_offline_geocoder()`.
    """
    resolved_geocoder = geocoder if geocoder is not None else get_offline_geocoder()
    indices = _query_indices(resolved_geocoder, lat, lon, count)

    candidates = []
    for index in indices:
        row = resolved_geocoder.locations[index]
        row_lat, row_lon = float(row["lat"]), float(row["lon"])
        candidates.append(
            PlaceCandidate(
                name=row["name"],
                admin1=row.get("admin1") or None,
                admin2=row.get("admin2") or None,
                country_code=row.get("cc") or None,
                lat=row_lat,
                lon=row_lon,
                distance_m=_haversine_m(lat, lon, row_lat, row_lon),
            )
        )
    return candidates


def reverse_geocode_offline(
    lat: float, lon: float, *, geocoder: OfflineGeocoderLike | None = None
) -> PlaceCandidate:
    """The single nearest offline gazetteer entry -- the city-level label the acceptance
    criterion requires. A thin wrapper over `candidate_places(..., count=1)`.

    Every coordinate resolves to *something*: the underlying k-d tree always returns its
    nearest neighbour, even for a mid-ocean point far from any city, so this never raises for
    a valid `(lat, lon)`. Distance then tells the caller how far that "nearest city" really is.
    """
    return candidate_places(lat, lon, count=1, geocoder=geocoder)[0]


class NominatimClient:
    """Thin, rate-limited wrapper over the Nominatim reverse-geocoding HTTP API.

    Optional and off by default (`config.geocode.use_nominatim`). Only ever adds a POI-level
    name on top of the offline city/region/country -- never required, and a failure here just
    means the offline label stands. Every network call funnels through `_call`, so a test can
    mock exactly one method and prove no real request happens.
    """

    def __init__(
        self,
        config: GeocodeConfig,
        *,
        sleep: Any = None,
        monotonic: Any = None,
    ) -> None:
        self._config = config
        # Resolved at construction time rather than bound as a mutable default -- a default
        # parameter value would capture `time.sleep` once at class-definition time, which a test
        # patching `story_book.pipeline.geocode.time.sleep` afterward could never reach.
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._last_call_at: float | None = None

    def _wait_for_rate_limit(self, now: float) -> None:
        if self._last_call_at is None:
            return
        elapsed = now - self._last_call_at
        remaining = self._config.nominatim_min_interval_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def reverse(self, lat: float, lon: float) -> str | None:
        """POI-level display name for a coordinate, or `None` if Nominatim has nothing useful
        or the request failed. Never raises -- a POI name is a nice-to-have, not a requirement."""
        now = self._monotonic()
        self._wait_for_rate_limit(now)
        self._last_call_at = now
        try:
            payload = self._call(lat, lon)
        except (OSError, ValueError) as exc:
            logger.warning("geocode: Nominatim lookup failed for (%.5f, %.5f): %s", lat, lon, exc)
            return None
        name = (payload or {}).get("name")
        return name or None

    def _call(self, lat: float, lon: float) -> dict[str, Any]:
        """The one place this class touches the network. Always mocked in tests."""
        params = urlencode({"lat": lat, "lon": lon, "format": "jsonv2"})
        request = urllib.request.Request(
            f"{NOMINATIM_REVERSE_URL}?{params}",
            headers={"User-Agent": self._config.nominatim_user_agent},
        )
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


def _get_place(conn: Connection, lat_key: float, lon_key: float, source: str) -> Place | None:
    row = conn.execute(
        "SELECT * FROM place WHERE lat_key = ? AND lon_key = ? AND source = ?",
        (lat_key, lon_key, source),
    ).fetchone()
    if row is None:
        return None
    return Place(
        id=row["id"],
        lat_key=row["lat_key"],
        lon_key=row["lon_key"],
        source=row["source"],
        poi=row["poi"],
        city=row["city"],
        region=row["region"],
        country=row["country"],
    )


def _delete_orphaned_places(conn: Connection) -> None:
    """Drop `place` rows nothing references any more.

    Keeps `--force geocode` honest: forcing a stage should give the same answer as a fresh build.
    Without this, rows written under older rules survive -- an earlier version keyed place identity
    on an ~11 m coordinate cell and left 159 rows all saying "Vienna", and a forced re-run silently
    inherited them because the coordinate cache resolved to the stale rows first.

    Only rows with no media *and* no event referencing them are removed. An event's `place_id` is
    real downstream work; deleting a place out from under it would be the same mistake as
    cascade-deleting a day that already has events.
    """
    conn.execute(
        """
        DELETE FROM place
        WHERE id NOT IN (SELECT place_id FROM media WHERE place_id IS NOT NULL)
          AND id NOT IN (SELECT place_id FROM event WHERE place_id IS NOT NULL)
        """
    )


def _upsert_place(conn: Connection, place: Place) -> int:
    """Find-or-create the `place` row for this *resolved place*, and return its id.

    Identity is the resolved content -- poi/city/region/country/source -- not the coordinate cell
    that happened to find it. A place is "Vienna", not "the square metre where we looked Vienna up".

    The coordinate cell is still a useful *lookup* cache (it avoids re-querying the geocoder), and
    `lat_key`/`lon_key` are retained as the first coordinate that resolved to this place. But
    keying row identity on them produced 159 rows all saying "Vienna" for one real trip, because
    the default rounding is ~11 m. That is not merely wasteful: `event.place_id` points here, so two
    events in the same square would carry different place ids for the same city, and any grouping or
    labelling by place would fragment.

    Raw SQL against `place` is explicitly allowed by the frozen contract (never against `media` or
    `stage_result`).
    """
    existing = conn.execute(
        """
        SELECT id FROM place
        WHERE source = :source
          AND IFNULL(poi, '') = IFNULL(:poi, '')
          AND IFNULL(city, '') = IFNULL(:city, '')
          AND IFNULL(region, '') = IFNULL(:region, '')
          AND IFNULL(country, '') = IFNULL(:country, '')
        """,
        {
            "source": place.source,
            "poi": place.poi,
            "city": place.city,
            "region": place.region,
            "country": place.country,
        },
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO place (lat_key, lon_key, poi, city, region, country, source)
        VALUES (:lat_key, :lon_key, :poi, :city, :region, :country, :source)
        ON CONFLICT (lat_key, lon_key, source) DO UPDATE SET
            poi = excluded.poi,
            city = excluded.city,
            region = excluded.region,
            country = excluded.country
        """,
        {
            "lat_key": place.lat_key,
            "lon_key": place.lon_key,
            "poi": place.poi,
            "city": place.city,
            "region": place.region,
            "country": place.country,
            "source": place.source,
        },
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = conn.execute(
        "SELECT id FROM place WHERE lat_key = ? AND lon_key = ? AND source = ?",
        (place.lat_key, place.lon_key, place.source),
    ).fetchone()
    return int(row["id"])


class GeocodeStage(WholeTripStage):
    """Resolve every located media item's coordinate to a `place` row and set `media.place_id`.

    See the module docstring for the offline-first / optional-Nominatim design. Caches by
    coordinate rounded to `config.geocode.coordinate_rounding_decimals`: hundreds of photos
    taken at one spot share one `place` row and cost one offline lookup (and, if enabled, one
    Nominatim call), both within a single run and across runs via the `place` table itself.
    """

    name = "geocode"
    version = 1
    # Aggregate over the whole media set: a cached 'ok' would go stale the moment scan adds a
    # photo, exactly the bug that hit `timezones` (see pipeline/base.py's Stage.always_run).
    # Re-running is cheap -- the per-coordinate place cache means new items only cost a lookup
    # for genuinely new coordinates.
    always_run = True
    description = (
        "Resolve media coordinates to places (offline city/region/country, optional Nominatim POI)."
    )

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        return available()

    def run(self, ctx: StageContext) -> None:
        cfg = ctx.config.geocode
        decimals = cfg.coordinate_rounding_decimals
        geocoder = get_offline_geocoder()
        # --no-cloud must complete the whole pipeline with zero network calls even if a config
        # file left use_nominatim on -- --no-cloud wins.
        use_nominatim = cfg.use_nominatim and not ctx.no_cloud
        nominatim = NominatimClient(cfg) if use_nominatim else None
        source = "nominatim" if nominatim is not None else "offline"

        # In-memory cache for this run, on top of the `place` table's cross-run cache -- avoids
        # even the SELECT for the common case of many consecutive photos at one spot.
        resolved_place_ids: dict[tuple[float, float], int] = {}

        for media in iter_media(ctx.conn):
            if not media.has_gps:
                continue  # no coordinate to resolve; a later stage (T20) may still backfill one.
            assert media.lat is not None and media.lon is not None
            key = (round(media.lat, decimals), round(media.lon, decimals))
            place_id = resolved_place_ids.get(key)
            if place_id is None:
                place_id = self._resolve_place_id(ctx, geocoder, nominatim, source, key)
                resolved_place_ids[key] = place_id
            if media.place_id != place_id:
                media.place_id = place_id
                upsert_media(ctx.conn, media)

        _delete_orphaned_places(ctx.conn)

    def _resolve_place_id(
        self,
        ctx: StageContext,
        geocoder: OfflineGeocoderLike,
        nominatim: NominatimClient | None,
        source: str,
        key: tuple[float, float],
    ) -> int:
        lat_key, lon_key = key
        existing = _get_place(ctx.conn, lat_key, lon_key, source)
        if existing is not None:
            # A coordinate hit avoids the geocoder lookup, but must still resolve to the
            # *content-canonical* row rather than returning whatever row this cell created. An
            # earlier version returned it directly, which meant a forced re-run inherited rows
            # written under the old coordinate-keyed identity -- 159 of them, all "Vienna" -- and
            # the collapse never happened. Re-canonicalizing here makes the cache an optimization
            # rather than a source of identity.
            return _upsert_place(ctx.conn, existing)

        nearest = reverse_geocode_offline(lat_key, lon_key, geocoder=geocoder)
        poi = nominatim.reverse(lat_key, lon_key) if nominatim is not None else None
        place = Place(
            id=None,
            lat_key=lat_key,
            lon_key=lon_key,
            source=source,
            poi=poi,
            city=nearest.name,
            region=nearest.admin1,
            country=nearest.country_code,
        )
        return _upsert_place(ctx.conn, place)
