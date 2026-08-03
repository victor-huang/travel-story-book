"""Module 2: timezone resolution -- the highest-risk logic in the project.

`DateTimeOriginal` is a naive local timestamp with no zone. Getting the zone wrong silently
corrupts the primary organizing axis: day boundaries land in the wrong place and photos from
two devices interleave incorrectly. This stage resolves, for every dated `media` row, a naive
local time, a resolved UTC instant, an IANA zone name, its offset in minutes, and which of four
levels supplied it -- **revised after real-data profiling** (`dev_plan/p01_profile_findings.md`):
a real export had items whose `OffsetTimeOriginal` sat 9 hours from the offset their own GPS
implies -- a phone still set to its home zone, or a re-export carrying the editing machine's
offset. So the tag is a hint, never ground truth:

1. `OffsetTimeOriginal`, but only if it agrees with the offset implied by the item's own GPS.
2. Timezone from GPS via `timezonefinder` (offline) + `zoneinfo`. On disagreement with the offset
   tag the zone always comes from GPS, but *which instant the photo records* is decided against
   its neighbours -- see `_gps_conflict`. One symptom has two causes, and getting this wrong moves
   photographs by hours.
3. Timezone inferred from the nearest-in-time GPS-bearing item on the *same* device.
4. `config.time.default_timezone` (or a per-device `DeviceConfig.default_timezone` override).

**Only a *measured* position counts as this item's own GPS** (`_has_measured_gps`). `gps_backfill`
runs after this stage, so on a second build it has already interpolated coordinates for items that
had none -- and treating those as evidence made the same source tree resolve to different times on
re-run. Interpolated coordinates take level 3.

Contract note for the integrator -- **this is a real gap, not a guess**: the frozen `Media`
dataclass has no field for the raw `OffsetTimeOriginal` tag, and as currently written
`pipeline/metadata.py` (T11) computes `ExifTimestamp.offset_minutes` but never persists it --
`tz_offset_minutes`/`tz_source` are left at their defaults. Per the task brief for this module,
T12 must not import `pipeline/metadata.py` or `story_book.exif`, so this stage instead treats the
existing frozen fields as the handoff channel: **a `media` row with `tz_source ==
TzSource.EXIF_OFFSET` and `tz_offset_minutes` set is read as "T11 saw an `OffsetTimeOriginal` tag
worth `tz_offset_minutes` minutes east of UTC"**. Until T11 is updated to set exactly those two
fields when the tag is present, level 1 above can never fire in production -- GPS (level 2) and
the other fallbacks still work correctly, and once T11 sets those fields no further change is
needed here. See the final task summary for the one-line addition `metadata.py` needs.

Also applies a per-device clock correction (`DeviceConfig.clock_offset_minutes`, added to the
naive local reading) for cameras whose clock was never set to local time, and warns loudly when
a device's timestamps look like they carry an *undeclared* clock offset, by comparing its
resolved instants against time-adjacent GPS-bearing items from other devices.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from story_book.config import Config, DeviceConfig
from story_book.db.connection import iter_media, upsert_media
from story_book.db.models import GpsSource, Media, TzSource
from story_book.pipeline.base import StageContext, WholeTripStage

logger = logging.getLogger(__name__)

# How tightly a cluster of cross-device deltas must agree, and how large the mean delta must be,
# before we call it a suspected undeclared clock offset rather than ordinary noise. Not config
# fields: config.py is a frozen contract owned by another task, and these are diagnostic
# heuristics rather than pipeline behavior -- see the final task summary.
_CLOCK_OFFSET_SPREAD_TOLERANCE_MINUTES = 3.0
_CLOCK_OFFSET_SUSPECT_THRESHOLD_MINUTES = 5.0
_CLOCK_OFFSET_MIN_SAMPLES = 2


class TimezoneFinderLike(Protocol):
    """The one method of `timezonefinder.TimezoneFinder` this module relies on."""

    def timezone_at(self, *, lat: float, lng: float) -> str | None: ...


def get_timezone_finder() -> TimezoneFinderLike:
    """Real, offline `timezonefinder.TimezoneFinder`. A separate function so tests can patch it
    without ever loading its (large) bundled geometry data from disk."""
    from timezonefinder import TimezoneFinder

    return TimezoneFinder()


@dataclass(slots=True)
class _Anchor:
    """A resolved instant this stage trusts enough to use as evidence for another item."""

    local: datetime
    """Naive local time, after any device clock correction."""

    utc: datetime
    tz_name: str | None
    tz_offset_minutes: int
    device_id: str | None
    has_gps: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One reading of a photo whose offset tag and GPS zone disagree."""

    kind: str  # "tag_instant" | "wall_local"
    local: datetime
    utc: datetime
    offset: int


@dataclass(frozen=True, slots=True)
class _Conflict:
    zone_name: str
    tag_offset: int
    gps_offset: int
    wall_reading: datetime
    tag_instant: _Candidate
    wall_local: _Candidate


NEIGHBOUR_WINDOW = timedelta(hours=6)
"""How close a same-device anchor must be to count as evidence for a conflicted frame.

Beyond this, the nearest confident photo says nothing useful about which reading is right, and
guessing from it would be worse than falling back to the documented default.
"""


def _parse_local(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _format_local(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _format_utc(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat(timespec="seconds")


def _device_config(config: Config, device_id: str | None) -> DeviceConfig:
    if device_id is None:
        return DeviceConfig()
    return config.devices.get(device_id, DeviceConfig())


def _offset_minutes_for(zone_name: str, local_naive: datetime) -> int:
    """Offset of an IANA zone at a given wall-clock instant, DST-aware (never a fixed offset)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        delta = local_naive.replace(tzinfo=ZoneInfo(zone_name)).utcoffset()
    except ZoneInfoNotFoundError:
        logger.warning("timezones: unknown IANA zone %r -- treating as UTC", zone_name)
        return 0
    assert delta is not None
    return int(delta.total_seconds() // 60)


def _has_measured_gps(media: Media) -> bool:
    """Whether this item's coordinates are its *own* evidence.

    `media.has_gps` is only "lat and lon are set", which after `gps_backfill` is also true of items
    whose position was **interpolated from neighbours**. Using those here would be circular -- the
    interpolation is derived from the very timestamps this stage produces -- and worse, it makes the
    stage's answer depend on whether a later stage has already run:

      * build 1: a GoPro clip has no coordinates -> resolved from a same-device neighbour.
      * build 2: `gps_backfill` has filled them in -> resolved as GPS-backed, an hour adrift.

    Same source tree, two different timestamps. So an interpolated position is treated as no
    position here, sending the item down the neighbour path where it belongs -- and the answer is
    then the same on every run. `EXIF` is a measurement and `MANUAL` is a human statement; both
    stand. Only `INTERPOLATED` is excluded, rather than requiring a positive marker, so an item of
    unknown provenance keeps whatever behaviour it had.
    """
    return media.has_gps and media.gps_source is not GpsSource.INTERPOLATED


def _gps_zone(finder: TimezoneFinderLike, media: Media) -> str | None:
    assert media.lat is not None and media.lon is not None
    return finder.timezone_at(lat=media.lat, lng=media.lon)


def _resolve_gps_backed(
    media: Media, config: Config, finder: TimezoneFinderLike, clock_offset: int
) -> tuple[datetime, datetime, str | None, int, TzSource]:
    """Level 1 (validated) / level 2. `media` has its own GPS fix."""
    corrected_local = _parse_local(media.exif_local or media.taken_local) + timedelta(
        minutes=clock_offset
    )

    zone_name = _gps_zone(finder, media)
    if zone_name is None:
        logger.warning(
            "timezones: %s has GPS (%.4f, %.4f) but timezonefinder could not place it -- "
            "falling back to the trip default timezone",
            media.hash,
            media.lat,
            media.lon,
        )
        zone_name = config.time.default_timezone
    gps_offset = _offset_minutes_for(zone_name, corrected_local)

    candidate_offset = media.exif_offset_minutes
    if candidate_offset is not None and candidate_offset == gps_offset:
        return (
            corrected_local,
            corrected_local - timedelta(minutes=gps_offset),
            zone_name,
            gps_offset,
            TzSource.EXIF_OFFSET,
        )

    if candidate_offset is None:
        # No tag at all. The wall reading is the best we have; read it in the GPS zone.
        return (
            corrected_local,
            corrected_local - timedelta(minutes=gps_offset),
            zone_name,
            gps_offset,
            TzSource.GPS,
        )

    raise AssertionError("conflicts are resolved by _resolve_conflict, not here")


def _gps_conflict(
    media: Media, config: Config, finder: TimezoneFinderLike, clock_offset: int
) -> _Conflict | None:
    """The two readings of a GPS-bearing photo whose offset tag disagrees with its zone.

    `None` when there is nothing to disagree about. Otherwise both interpretations, because
    **one symptom has two causes** and the tag alone cannot say which:

      * *The camera clock really was on another zone.* A phone still set to its home time wrote
        `08:26 -07:00` in Vienna. The wall reading is meaningless locally; the tag gives the true
        instant, 15:26 UTC = 17:26 Vienna. -> `tag_instant`.
      * *The clock was already local and only the tag is stale.* The same phone wrote
        `15:59 -07:00` while reading Vienna time. Here the wall reading is correct and applying
        the tag throws the photo nine hours forward, past midnight. -> `wall_local`.

    Both were present in one real trip: of 15 conflicted frames, 8 were the first kind and 6 the
    second. Picking either rule unconditionally corrupts the other group, so the choice is made
    against same-device neighbours in `_resolve_conflict`.
    """
    wall_reading = _parse_local(media.exif_local or media.taken_local) + timedelta(
        minutes=clock_offset
    )
    tag_offset = media.exif_offset_minutes
    if tag_offset is None:
        return None

    zone_name = _gps_zone(finder, media) or config.time.default_timezone
    gps_offset = _offset_minutes_for(zone_name, wall_reading)
    if tag_offset == gps_offset:
        return None

    tag_utc = wall_reading - timedelta(minutes=tag_offset)
    tag_display_offset = _offset_minutes_for(zone_name, tag_utc + timedelta(minutes=gps_offset))
    tag_instant = _Candidate(
        "tag_instant",
        tag_utc + timedelta(minutes=tag_display_offset),
        tag_utc,
        tag_display_offset,
    )
    wall_local = _Candidate(
        "wall_local",
        wall_reading,
        wall_reading - timedelta(minutes=gps_offset),
        gps_offset,
    )
    return _Conflict(zone_name, tag_offset, gps_offset, wall_reading, tag_instant, wall_local)


def _resolve_conflict(
    media: Media, conflict: _Conflict, anchors: list[_Anchor]
) -> tuple[datetime, datetime, str | None, int, TzSource]:
    """Pick the reading that sits nearest a confident photo from the same camera.

    The camera's own wall clock is no help -- it is the thing in doubt, and it is not even
    monotonic when a device flips between two zone settings mid-trip. What *is* reliable is that a
    photo belongs near the ones taken around it. So: compute both instants, and keep whichever
    lands closer to a same-device photo whose tag and GPS already agreed.

    With no anchor inside `NEIGHBOUR_WINDOW` there is no evidence either way, and the documented
    default (trust the tag for the instant) stands -- announced as unverified rather than settled.
    """
    candidates = (conflict.tag_instant, conflict.wall_local)
    gaps: dict[str, float] = {}
    for candidate in candidates:
        nearest = min(
            (abs((a.utc - candidate.utc).total_seconds()) for a in anchors), default=float("inf")
        )
        gaps[candidate.kind] = nearest

    best = min(candidates, key=lambda c: gaps[c.kind])
    if gaps[best.kind] > NEIGHBOUR_WINDOW.total_seconds():
        chosen, verdict = conflict.tag_instant, "no nearby photo from this camera to check against"
    else:
        chosen = best
        other = next(c for c in candidates if c.kind != chosen.kind)
        verdict = (
            f"nearest same-camera photo is {gaps[chosen.kind] / 60:.0f} min away, "
            f"against {gaps[other.kind] / 60:.0f} min for the alternative"
        )

    logger.warning(
        "timezones: %s -- EXIF offset %+d min disagrees with its GPS zone %s (%+d min). "
        "Reading %s as %s local (%s).",
        media.hash,
        conflict.tag_offset,
        conflict.zone_name,
        conflict.gps_offset,
        conflict.wall_reading.isoformat(timespec="minutes"),
        chosen.local.isoformat(timespec="minutes"),
        verdict,
    )
    return chosen.local, chosen.utc, conflict.zone_name, chosen.offset, TzSource.GPS


def _nearest_anchor(anchors: list[_Anchor], local_time: datetime) -> _Anchor | None:
    if not anchors:
        return None
    return min(anchors, key=lambda a: abs((a.local - local_time).total_seconds()))


def _resolve_without_gps(
    media: Media,
    config: Config,
    clock_offset: int,
    device_anchors: list[_Anchor],
    trip_anchors: list[_Anchor] | None = None,
) -> tuple[datetime, datetime, str | None, int, TzSource]:
    """Levels 3/4. `media` has no GPS fix of its own."""
    corrected_local = _parse_local(media.exif_local or media.taken_local) + timedelta(
        minutes=clock_offset
    )

    neighbor = _nearest_anchor(device_anchors, corrected_local) if media.device_id else None
    if neighbor is not None:
        tz_name = neighbor.tz_name
        offset = neighbor.tz_offset_minutes
        tz_source = TzSource.DEVICE_NEIGHBOR
    else:
        device_cfg = _device_config(config, media.device_id)
        tz_name = device_cfg.default_timezone or config.time.default_timezone
        offset = _offset_minutes_for(tz_name, corrected_local)
        tz_source = TzSource.CONFIG

    tagged = _offset_tag_reading(media, corrected_local, tz_name, offset)
    if tagged is not None:
        anchors = device_anchors or (trip_anchors or [])
        chosen = _prefer_anchored(tagged, corrected_local, offset, anchors)
        if chosen is not None:
            logger.warning(
                "timezones: %s has no GPS -- reading its %+d min offset tag as the instant: "
                "%s -> %s local, which lands beside the rest of the trip (the wall reading does "
                "not).",
                media.hash,
                media.exif_offset_minutes,
                corrected_local.isoformat(timespec="minutes"),
                chosen.local.isoformat(timespec="minutes"),
            )
            return chosen.local, chosen.utc, tz_name, chosen.offset, tz_source

    taken_utc = corrected_local - timedelta(minutes=offset)
    return corrected_local, taken_utc, tz_name, offset, tz_source


def _offset_tag_reading(
    media: Media, wall_reading: datetime, tz_name: str, zone_offset: int
) -> _Candidate | None:
    """The instant the offset tag implies, rendered in `tz_name`. `None` if there is no tag.

    With no GPS the tag is the *only* evidence about which instant the wall reading names, and
    discarding it means assuming the clock was already on the display zone. On a real trip 11
    paragliding clips carried `-07:00` with no GPS and no device id: read as local they sat at
    02:37, six hours from anything else that day; read through the tag they sit at 11:37, among the
    photographs from the same activity.
    """
    tag = media.exif_offset_minutes
    if tag is None or tag == zone_offset:
        return None
    utc = wall_reading - timedelta(minutes=tag)
    display_offset = _offset_minutes_for(tz_name, utc + timedelta(minutes=zone_offset))
    return _Candidate("tag_instant", utc + timedelta(minutes=display_offset), utc, display_offset)


def _prefer_anchored(
    tagged: _Candidate, wall_reading: datetime, zone_offset: int, anchors: list[_Anchor]
) -> _Candidate | None:
    """`tagged` if it sits near the rest of the trip and the wall reading does not, else `None`.

    Deliberately one-directional and conservative. A photograph really taken at 02:37 with a stale
    tag must not be dragged into the middle of the day just because daytime is busier, so the tag
    only wins when the wall reading has *no* neighbour inside `NEIGHBOUR_WINDOW` and the tagged
    reading does.
    """
    if not anchors:
        return None
    window = NEIGHBOUR_WINDOW.total_seconds()
    wall_utc = wall_reading - timedelta(minutes=zone_offset)
    gap_wall = min(abs((a.utc - wall_utc).total_seconds()) for a in anchors)
    gap_tagged = min(abs((a.utc - tagged.utc).total_seconds()) for a in anchors)
    return tagged if gap_tagged <= window < gap_wall else None


def resolve_timezones(
    media_list: list[Media], config: Config, finder: TimezoneFinderLike
) -> list[Media]:
    """Pure resolution over an in-memory list of media. No DB, no filesystem.

    Returns the subset of `media_list` that had a `taken_local` to resolve, each mutated in
    place with `taken_local` (clock-corrected), `taken_utc`, `tz_name`, `tz_offset_minutes`,
    and `tz_source` set. Undated items are left untouched and excluded from the result.
    """
    dated = [m for m in media_list if m.taken_local]

    device_anchors: dict[str, list[_Anchor]] = defaultdict(list)
    trip_anchors: list[_Anchor] = []
    """Every resolved instant, whatever camera it came from. `device_anchors` answers "what was
    *this* camera's clock doing"; this answers "when did the trip happen", which is the only
    question available for an item with no device id at all."""
    resolved: list[Media] = []

    def _apply(media: Media, local, utc, tz_name, offset, source, *, anchor: bool) -> None:
        media.taken_local = _format_local(local)
        media.taken_utc = _format_utc(utc)
        media.tz_name = tz_name
        media.tz_offset_minutes = offset
        media.tz_source = source
        resolved.append(media)
        if not anchor:
            return
        entry = _Anchor(
            local, utc, tz_name, offset, media.device_id, has_gps=_has_measured_gps(media)
        )
        trip_anchors.append(entry)
        if media.device_id:
            device_anchors[media.device_id].append(entry)

    # Pass 1: GPS items whose offset tag and zone agree (or that have no tag). Only these become
    # anchors, because pass 2 needs evidence that is not itself in question.
    deferred: list[tuple[Media, _Conflict]] = []
    for media in dated:
        if not _has_measured_gps(media):
            continue
        clock_offset = _device_config(config, media.device_id).clock_offset_minutes
        conflict = _gps_conflict(media, config, finder, clock_offset)
        if conflict is not None:
            deferred.append((media, conflict))
            continue
        _apply(
            media,
            *_resolve_gps_backed(media, config, finder, clock_offset),
            anchor=True,
        )

    # Pass 2: the conflicted ones, decided against those anchors. They are added as anchors only
    # afterwards, so one uncertain frame can never be the evidence for another.
    for media, conflict in deferred:
        _apply(
            media,
            *_resolve_conflict(media, conflict, device_anchors.get(media.device_id, [])),
            anchor=False,
        )
    for media, _ in deferred:
        if media.taken_local and media.taken_utc:
            entry = _Anchor(
                _parse_local(media.taken_local),
                datetime.fromisoformat(media.taken_utc).replace(tzinfo=None),
                media.tz_name,
                media.tz_offset_minutes or 0,
                media.device_id,
                has_gps=True,
            )
            trip_anchors.append(entry)
            if media.device_id:
                device_anchors[media.device_id].append(entry)

    # Pass 3: everything else falls back to a same-device neighbor, then config.
    for media in dated:
        if _has_measured_gps(media):
            continue
        clock_offset = _device_config(config, media.device_id).clock_offset_minutes
        anchors = device_anchors.get(media.device_id, []) if media.device_id else []
        # An item with no device of its own gets the whole trip as context: the question is when
        # this happened relative to everything else, not what one camera's clock was doing.
        local, utc, tz_name, offset, source = _resolve_without_gps(
            media, config, clock_offset, anchors, trip_anchors=trip_anchors
        )
        _apply(media, local, utc, tz_name, offset, source, anchor=True)

    warn_suspected_clock_offsets(resolved, config)
    return resolved


def warn_suspected_clock_offsets(resolved: list[Media], config: Config) -> None:
    """Compare each device's resolved instants against time-adjacent GPS-bearing items from
    *other* devices. A device whose clock was never set to local time (or set to the wrong
    time entirely) tends to sit a roughly constant number of minutes away from what nearby
    other-device evidence implies -- ordinary noise does not cluster this tightly.

    Pure and side-effect-free except for `logger.warning`; does not touch `media` rows. Users
    act on the warning by adding `devices."<id>".clock_offset_minutes` to config and rerunning.
    """
    by_device: dict[str, list[datetime]] = defaultdict(list)
    other_device_gps_utc: list[tuple[datetime, str]] = []
    for media in resolved:
        if media.device_id is None or media.taken_utc is None:
            continue
        utc = datetime.fromisoformat(media.taken_utc)
        by_device[media.device_id].append(utc)
        if media.has_gps:
            other_device_gps_utc.append((utc, media.device_id))

    window = timedelta(minutes=config.time.gps_interpolation_window_minutes)
    for device_id, instants in by_device.items():
        if _device_config(config, device_id).clock_offset_minutes:
            continue  # already corrected in config

        deltas: list[float] = []
        for utc in instants:
            reference = _nearest_other_device(other_device_gps_utc, utc, device_id)
            if reference is None:
                continue
            gap = abs((reference - utc).total_seconds())
            if gap <= window.total_seconds():
                deltas.append((reference - utc).total_seconds() / 60)

        if len(deltas) < _CLOCK_OFFSET_MIN_SAMPLES:
            continue
        spread = max(deltas) - min(deltas)
        mean = sum(deltas) / len(deltas)
        if spread <= _CLOCK_OFFSET_SPREAD_TOLERANCE_MINUTES and (
            abs(mean) >= _CLOCK_OFFSET_SUSPECT_THRESHOLD_MINUTES
        ):
            logger.warning(
                "timezones: device %r looks like its clock was off by about %.0f minutes "
                "(consistent across %d item(s) compared against nearby GPS-bearing items from "
                "other devices). Consider adding devices.%r.clock_offset_minutes = %d to "
                "config and rerunning.",
                device_id,
                mean,
                len(deltas),
                device_id,
                round(mean),
            )


def _nearest_other_device(
    candidates: list[tuple[datetime, str]], utc: datetime, exclude_device: str
) -> datetime | None:
    others = [dt for dt, device_id in candidates if device_id != exclude_device]
    if not others:
        return None
    return min(others, key=lambda dt: abs((dt - utc).total_seconds()))


class TimezoneStage(WholeTripStage):
    """Resolve `taken_utc`, `tz_name`, `tz_offset_minutes`, and `tz_source` for every dated
    `media` row. See module docstring for the four-level resolution order."""

    name = "timezones"
    version = 2
    # Aggregate stages derive from the whole media set, so a cached result goes stale the moment
    # scan adds a file: the new item would keep a NULL taken_utc and drop out of ordering, day
    # grouping, and the timeline -- invisibly. Re-resolving is pure in-memory work over rows
    # already in the DB, so running it every build is cheap and idempotent.
    always_run = True
    description = "Resolve capture timezone and UTC instant for every dated media item."

    def run(self, ctx: StageContext) -> None:
        media_list = list(iter_media(ctx.conn))
        finder = get_timezone_finder()
        resolved = resolve_timezones(media_list, ctx.config, finder)
        for media in resolved:
            upsert_media(ctx.conn, media)
