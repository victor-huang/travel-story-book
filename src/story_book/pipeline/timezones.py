"""Module 2: timezone resolution -- the highest-risk logic in the project.

`DateTimeOriginal` is a naive local timestamp with no zone. Getting the zone wrong silently
corrupts the primary organizing axis: day boundaries land in the wrong place and photos from
two devices interleave incorrectly. This stage resolves, for every dated `media` row, a naive
local time, a resolved UTC instant, an IANA zone name, its offset in minutes, and which of four
levels supplied it -- **revised after real-data profiling** (`dev_plan/p01_profile_findings.md`):
a real 286-item export had 7 items whose `OffsetTimeOriginal` sat 9 hours from the offset their
own GPS implies (an edited/re-exported photo carrying the *editing machine's* offset). So the
tag is a hint, never ground truth, and GPS -- a measurement -- outranks it on disagreement:

1. `OffsetTimeOriginal`, but only if it agrees with the offset implied by the item's own GPS.
2. Timezone from GPS via `timezonefinder` (offline) + `zoneinfo`. Wins any disagreement with
   the EXIF offset tag; the conflict is logged loudly.
3. Timezone inferred from the nearest-in-time GPS-bearing item on the *same* device.
4. `config.time.default_timezone` (or a per-device `DeviceConfig.default_timezone` override).

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
from story_book.db.models import Media, TzSource
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


def _gps_zone(finder: TimezoneFinderLike, media: Media) -> str | None:
    assert media.lat is not None and media.lon is not None
    return finder.timezone_at(lat=media.lat, lng=media.lon)


def _resolve_gps_backed(
    media: Media, config: Config, finder: TimezoneFinderLike, clock_offset: int
) -> tuple[datetime, datetime, str | None, int, TzSource]:
    """Level 1 (validated) / level 2. `media` has its own GPS fix."""
    corrected_local = _parse_local(media.taken_local) + timedelta(minutes=clock_offset)

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

    # Tag and GPS disagree. The two signals answer *different questions*, and the earlier version
    # of this code threw one of them away:
    #
    #   * The offset tag says which zone the camera's clock was reading -- so it is the best
    #     evidence for the true UTC instant, even when it is "wrong" about where you were.
    #   * GPS says where the photo was taken -- so it decides which zone to *display* in.
    #
    # Discarding the tag and reading the wall time as GPS-local silently shifts the photo by the
    # size of the disagreement. Found by hand-labelling (P03): a phone still set to its home zone
    # recorded 08:26 -07:00 in Vienna. Reading that as 08:26 Vienna put it nine hours from its own
    # filename neighbours -- IMG_1880 at 17:26, IMG_1881 at 08:26, IMG_1883 at 17:26 -- for frames
    # shot seconds apart. The correct instant is 08:26 -07:00 = 15:26 UTC = 17:26 Vienna.
    #
    # So: take the instant from the tag, take the zone from GPS, and re-render the local time.
    taken_utc = corrected_local - timedelta(minutes=candidate_offset)
    display_offset = _offset_minutes_for(zone_name, taken_utc + timedelta(minutes=gps_offset))
    display_local = taken_utc + timedelta(minutes=display_offset)
    logger.warning(
        "timezones: %s -- EXIF offset %+d min disagrees with its GPS zone %s (%+d min). Using the "
        "tag for the instant and GPS for the zone: %s -> %s local.",
        media.hash,
        candidate_offset,
        zone_name,
        gps_offset,
        corrected_local.isoformat(timespec="minutes"),
        display_local.isoformat(timespec="minutes"),
    )
    return display_local, taken_utc, zone_name, display_offset, TzSource.GPS


def _nearest_anchor(anchors: list[_Anchor], local_time: datetime) -> _Anchor | None:
    if not anchors:
        return None
    return min(anchors, key=lambda a: abs((a.local - local_time).total_seconds()))


def _resolve_without_gps(
    media: Media,
    config: Config,
    clock_offset: int,
    device_anchors: list[_Anchor],
) -> tuple[datetime, datetime, str | None, int, TzSource]:
    """Levels 3/4. `media` has no GPS fix of its own."""
    corrected_local = _parse_local(media.taken_local) + timedelta(minutes=clock_offset)

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

    taken_utc = corrected_local - timedelta(minutes=offset)
    return corrected_local, taken_utc, tz_name, offset, tz_source


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
    resolved: list[Media] = []

    # Pass 1: items with their own GPS fix are resolved from evidence (levels 1/2) and become
    # anchors other items on the same device can borrow from.
    for media in dated:
        if not media.has_gps:
            continue
        clock_offset = _device_config(config, media.device_id).clock_offset_minutes
        local, utc, tz_name, offset, source = _resolve_gps_backed(
            media, config, finder, clock_offset
        )
        media.taken_local = _format_local(local)
        media.taken_utc = _format_utc(utc)
        media.tz_name = tz_name
        media.tz_offset_minutes = offset
        media.tz_source = source
        resolved.append(media)
        if media.device_id:
            device_anchors[media.device_id].append(
                _Anchor(local, utc, tz_name, offset, media.device_id, has_gps=True)
            )

    # Pass 2: everything else falls back to a same-device neighbor, then config.
    for media in dated:
        if media.has_gps:
            continue
        clock_offset = _device_config(config, media.device_id).clock_offset_minutes
        anchors = device_anchors.get(media.device_id, []) if media.device_id else []
        local, utc, tz_name, offset, source = _resolve_without_gps(
            media, config, clock_offset, anchors
        )
        media.taken_local = _format_local(local)
        media.taken_utc = _format_utc(utc)
        media.tz_name = tz_name
        media.tz_offset_minutes = offset
        media.tz_source = source
        resolved.append(media)
        if media.device_id:
            device_anchors[media.device_id].append(
                _Anchor(local, utc, tz_name, offset, media.device_id, has_gps=False)
            )

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
    version = 1
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
