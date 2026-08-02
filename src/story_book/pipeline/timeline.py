"""Module 12: the timeline builder, which writes `trip.json`.

`trip.json` is the canonical intermediate artifact. The HTML report (13) and the ChatGPT package
(14) both render *only* from it -- neither reaches back into the DB. That rule is what makes the
report a pure function of a file you can read, diff, and hand to a future Phase 2 consumer.

Four decisions are load-bearing.

**Assets have a stable public id.** P02's reviewer could not refer to a photo: contact-sheet cell
ids are *positional*, so "cell 14" means a different picture as soon as selection changes. The
pipeline has had a stable identity all along -- the BLAKE2b content hash -- and simply never
exposed it. `asset_id` is a short prefix of that hash, and every other structure in the file
refers to assets by it rather than repeating their data.

**Explicit negatives are recorded, not implied by absence.** A video with no `transcript` key
could mean "we listened and there was no speech" or "we never processed it", and those lead a
writer to opposite conclusions. `transcript_status` says which. The same reasoning gives every
day a `gps_coverage` and the trip a `privacy` block: a filter that did not run must not read as
a filter that found nothing.

**No database rowid is published.** `day`, `event` and `cluster` rows are deleted and rebuilt on
every run, so their autoincrement ids climb even when nothing changed -- a second build of an
identical library produced a completely different `trip.json`. Every published id is instead a
function of the media set: an event is `<date>#<seq>`, a cluster is its keeper's `asset_id`, and
the selection records rank and reason without a scope id, because the asset already says which day
and event it belongs to. This is the same reasoning that keeps those ids out of `overrides.toml`,
and the guarantee is asserted by a test that builds twice and diffs.

**Locations are described, not averaged away.** One centroid can place an event in the middle of
a square nobody stood in. Each event carries centroid, first and last fix, radius, coverage, and
-- when the event actually moved -- a simplified path.

**Nothing is invented.** Absent data is `null` with a status beside it. There are no aesthetic or
composition scores here because the pipeline does not compute them, and a plausible number would
be worse than a missing one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

from story_book import __version__
from story_book.config import Config, TimelineConfig
from story_book.db.models import MediaKind, SelectionScope
from story_book.overrides import Overrides, resolve
from story_book.pipeline.base import StageContext, WholeTripStage
from story_book.pipeline.thumbnails import preview_relpath, thumbnail_relpath
from story_book.trip_context import TripContext

logger = logging.getLogger(__name__)

TRIP_JSON_SCHEMA_VERSION = 4
TRIP_JSON_FILENAME = "trip.json"

EARTH_RADIUS_M = 6_371_000.0


class TranscriptStatus:
    """Why a video has no transcript. The distinction P02 asked for, made explicit."""

    TRANSCRIBED = "transcribed"
    NO_SPEECH = "no_speech"
    """Processed, and nothing was found. A real negative result."""
    NOT_PROCESSED = "not_processed"
    """Never attempted -- no ffmpeg, `transcribe = "none"`, or the stage failed."""


def _haversine_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (*first, *second))
    inner = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(inner))


def _perpendicular_m(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Distance from `point` to the segment start-end, in metres.

    Latitude/longitude are projected to a local plane first. Over a single event -- a few hundred
    metres at most -- the error from treating that plane as flat is far below the simplification
    tolerance, and it avoids a geodesic dependency for a cosmetic calculation.
    """
    scale = cos(radians(start[0]))
    px, py = point[1] * scale, point[0]
    ax, ay = start[1] * scale, start[0]
    bx, by = end[1] * scale, end[0]
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return _haversine_m(point, start)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    nearest = (ay + t * dy, (ax + t * dx) / scale)
    return _haversine_m(point, nearest)


def simplify_path(
    points: list[tuple[float, float]], tolerance_m: float
) -> list[tuple[float, float]]:
    """Douglas-Peucker. One point per photo is 121 points for one afternoon, mostly jitter."""
    if len(points) < 3:
        return list(points)
    first, last = points[0], points[-1]
    index, worst = 0, 0.0
    for i in range(1, len(points) - 1):
        distance = _perpendicular_m(points[i], first, last)
        if distance > worst:
            index, worst = i, distance
    if worst <= tolerance_m:
        return [first, last]
    left = simplify_path(points[: index + 1], tolerance_m)
    right = simplify_path(points[index:], tolerance_m)
    return left[:-1] + right


def build_asset_ids(hashes: list[str], config: TimelineConfig) -> dict[str, str]:
    """Short, stable public ids: a prefix of each content hash.

    The prefix lengthens until every id is unique rather than truncating into a collision, so an
    unlucky trip produces longer ids instead of two photos sharing one name.
    """
    ordered = sorted(hashes)
    length = max(4, config.asset_id_length)
    while length < len(ordered[0] if ordered else ""):
        ids = {h[:length] for h in ordered}
        if len(ids) == len(ordered):
            break
        length += 2
    return {h: h[:length] for h in ordered}


def local_with_offset(taken_local: str | None, offset_minutes: int | None) -> str | None:
    """`2026-07-18T11:03:22` + 120 -> `2026-07-18T11:03:22+02:00`.

    A bare local timestamp is ambiguous the moment a trip crosses a zone, and a consumer comparing
    it against an itinerary has no way to know which. The offset is carried inline so the string is
    self-describing; the IANA name and the UTC instant sit beside it.
    """
    if taken_local is None:
        return None
    if offset_minutes is None:
        return taken_local
    sign = "+" if offset_minutes >= 0 else "-"
    total = abs(offset_minutes)
    return f"{taken_local}{sign}{total // 60:02d}:{total % 60:02d}"


def geometry(width: int | None, height: int | None) -> dict[str, Any]:
    """Orientation and aspect ratio, for layout.

    A renderer that does not know the shape of a frame will propose a panoramic hero for a portrait
    photograph. Square is its own case rather than a rounding of one of the other two.
    """
    if not width or not height:
        return {"width": width, "height": height, "orientation": None, "aspect_ratio": None}
    ratio = width / height
    if abs(ratio - 1.0) < 0.02:
        orientation = "square"
    elif ratio > 1.0:
        orientation = "landscape"
    else:
        orientation = "portrait"
    return {
        "width": width,
        "height": height,
        "orientation": orientation,
        "aspect_ratio": round(ratio, 4),
    }


def keyframe_seconds(duration: float | None, count: int) -> list[float]:
    """Where each extracted keyframe sits in the clip.

    Mirrors `video._keyframe_timestamps` -- evenly spaced midpoints. Recomputed rather than stored
    because it is a pure function of duration and count, and a stored copy could disagree with the
    frames actually on disk.
    """
    if not duration or count <= 0:
        return []
    return [round(duration * (i + 0.5) / count, 2) for i in range(count)]


def _minutes_between(start: str | None, end: str | None) -> float | None:
    """A duration, computed from UTC instants only.

    Never from local wall time. Once `taken_local` carries an offset, a library where some items
    have a resolved offset and some do not mixes aware and naive datetimes and raises -- and even
    where it does not raise, subtracting two wall times across a zone change gives the wrong
    answer. This is the project's standing rule: order by UTC, split days by local.
    """
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60.0


def _fetch_places(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {
        row["id"]: {
            "id": row["id"],
            "poi": row["poi"],
            "city": row["city"],
            "region": row["region"],
            "country": row["country"],
            "lat": row["lat_key"],
            "lon": row["lon_key"],
            "source": row["source"],
        }
        for row in conn.execute("SELECT * FROM place")
    }


def _fetch_landmarks(conn: sqlite3.Connection) -> tuple[dict[int, dict], dict[str, list[int]]]:
    landmarks = {
        row["id"]: {
            "id": row["id"],
            "name": row["name"],
            "confidence": row["confidence"],
            "description": row["description"],
            "source": row["source"],
        }
        for row in conn.execute("SELECT * FROM landmark")
    }
    by_media: dict[str, list[int]] = {}
    for row in conn.execute("SELECT media_hash, landmark_id FROM media_landmark"):
        by_media.setdefault(row["media_hash"], []).append(row["landmark_id"])
    return landmarks, by_media


def _fetch_video(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    meta = {}
    for row in conn.execute("SELECT * FROM video_meta"):
        meta[row["media_hash"]] = {
            "fps": row["fps"],
            "poster": row["poster_path"],
            "keyframes": json.loads(row["keyframe_paths"]) if row["keyframe_paths"] else [],
            "motion_score": row["motion_score"],
            "mean_volume_db": row["mean_volume_db"],
        }
    return meta


def _fetch_transcripts(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["media_hash"]: {
            "model": row["model"],
            "text": row["text"],
            "segments": json.loads(row["segments"]) if row["segments"] else None,
        }
        for row in conn.execute("SELECT * FROM transcript")
    }


def _fetch_selection(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, Any]]]:
    """Rank and reason per scope. The scope *id* is deliberately omitted: it is a rowid, and the
    asset already carries the day and event it was chosen within."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM selection"):
        out.setdefault(row["media_hash"], {})[row["scope"]] = {
            "rank": row["rank"],
            "reason": row["reason"],
        }
    return out


def _fetch_clusters(conn: sqlite3.Connection, ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    """A cluster is identified by its keeper's `asset_id`, not by its rowid.

    Clusters are rebuilt from scratch every run, so the rowid is meaningless across builds. The
    keeper is content-derived and is also the more useful thing to name: "these are the shots
    that lost to <id>".
    """
    out = {}
    for row in conn.execute(
        "SELECT mc.media_hash, c.kind, c.keeper_hash "
        "FROM media_cluster mc JOIN cluster c ON c.id = mc.cluster_id"
    ):
        out[row["media_hash"]] = {
            "id": ids.get(row["keeper_hash"]),
            "kind": row["kind"],
            "is_keeper": row["keeper_hash"] == row["media_hash"],
        }
    return out


def _video_block(
    media_hash: str,
    duration: float | None,
    video_meta: dict[str, dict],
    transcripts: dict[str, dict],
    processed: bool,
    short_clip_max_seconds: float = 2.0,
) -> dict[str, Any]:
    meta = video_meta.get(media_hash)
    transcript = transcripts.get(media_hash)
    if transcript is not None:
        status = TranscriptStatus.TRANSCRIBED
    elif processed:
        status = TranscriptStatus.NO_SPEECH
    else:
        status = TranscriptStatus.NOT_PROCESSED
    paths = (meta or {}).get("keyframes", [])
    seconds = keyframe_seconds(duration, len(paths))
    short = duration is not None and duration < short_clip_max_seconds
    if short:
        # Five frames sampled across 0.37 seconds are five views of one instant.
        paths, seconds = paths[:1], seconds[:1]
    return {
        "duration_seconds": duration,
        # Stated by duration, not inferred to be a Live Photo: these files carry no
        # `ContentIdentifier`, so naming one would be a guess wearing a fact's clothes.
        "subtype": "short_clip" if short else "clip",
        "storyboard_candidate": not short,
        "fps": (meta or {}).get("fps"),
        "poster": (meta or {}).get("poster"),
        # Paths *and* timestamps. One contact-sheet cell cannot represent a 112-second clip, so an
        # editor needs to know what happens across it and at what offset -- otherwise a suggested
        # duration says how long to use the footage but not which part.
        "keyframes": [
            {"seconds": at, "path": path} for at, path in zip(seconds, paths, strict=False)
        ],
        "motion_score": (meta or {}).get("motion_score"),
        "mean_volume_db": (meta or {}).get("mean_volume_db"),
        "transcript_status": status,
        "transcript": transcript,
    }


def _processed_video_hashes(conn: sqlite3.Connection) -> set[str]:
    """Videos the video stage genuinely finished, so 'no transcript' is a real negative."""
    return {
        row["media_hash"]
        for row in conn.execute(
            "SELECT media_hash FROM stage_result WHERE stage = 'video' AND status = 'ok'"
        )
    }


def public_event_id(local_date: str | None, seq: int | None) -> str | None:
    """`<date>#<seq>` -- a function of the media set, unlike the DB rowid.

    Event rows are dropped and rebuilt every run, so their autoincrement ids differ between two
    builds of an identical library. Publishing those would make `trip.json` churn on every build
    and would give the report a reference that silently retargets.
    """
    if local_date is None or seq is None:
        return None
    return f"{local_date}#{seq}"


def _derived_images(
    media_hash: str, kind: MediaKind, out_dir: Path | None, video_meta: dict[str, dict]
) -> tuple[str | None, str | None]:
    """Where the report and the package find pixels for this item.

    A video has no thumbnail of its own -- its poster frame, already extracted by the video
    stage, serves as both. Paths are relative to the output directory so the artifact stays
    portable; `None` means the derivative genuinely does not exist, which the report renders as
    a placeholder rather than a broken image.
    """
    if kind is MediaKind.VIDEO:
        poster = (video_meta.get(media_hash) or {}).get("poster")
        return poster, poster
    thumb, preview = thumbnail_relpath(media_hash), preview_relpath(media_hash)
    if out_dir is not None:
        thumb = thumb if (out_dir / thumb).exists() else None
        preview = preview if (out_dir / preview).exists() else None
    return thumb, preview


def _build_assets(
    conn: sqlite3.Connection,
    config: Config,
    out_dir: Path | None = None,
    rejected: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    places = _fetch_places(conn)
    _, landmarks_by_media = _fetch_landmarks(conn)
    video_meta = _fetch_video(conn)
    transcripts = _fetch_transcripts(conn)
    selection = _fetch_selection(conn)
    processed_videos = _processed_video_hashes(conn)

    rows = list(
        conn.execute(
            """
            SELECT m.*, s.overall, s.sharpness, s.exposure, s.contrast, s.face_count,
                   s.face_max_frac, s.content_class, e.seq AS event_seq, d.local_date
            FROM media m
            LEFT JOIN score s ON s.media_hash = m.hash
            LEFT JOIN media_event me ON me.media_hash = m.hash
            LEFT JOIN event e ON e.id = me.event_id
            LEFT JOIN day d ON d.id = e.day_id
            ORDER BY m.taken_utc, m.hash
            """
        )
    )
    ids = build_asset_ids([row["hash"] for row in rows], config.timeline)
    clusters = _fetch_clusters(conn, ids)

    assets: dict[str, dict[str, Any]] = {}
    for row in rows:
        # A rejected item leaves the artifact entirely, not just the highlights. `overrides.toml`
        # says "never include these", and a screenshot that still counts toward the day, drops a
        # pin on the map and invents a 00:59 stop has plainly been included. The file stays in the
        # library and in the database -- nothing is deleted, it is just not part of the story.
        if row["hash"] in rejected:
            continue
        kind = MediaKind(row["kind"])
        asset_id = ids[row["hash"]]
        asset: dict[str, Any] = {
            "asset_id": asset_id,
            "content_hash": row["hash"],
            "filename": row["path"].rsplit("/", 1)[-1],
            "kind": str(kind),
            "bytes": row["bytes"],
            "geometry": geometry(row["width"], row["height"]),
            "taken_local": local_with_offset(row["taken_local"], row["tz_offset_minutes"]),
            "taken_utc": row["taken_utc"],
            "timezone": {
                "name": row["tz_name"],
                "offset_minutes": row["tz_offset_minutes"],
                "source": row["tz_source"],
            },
            "day": row["local_date"],
            # The trip day it belongs to, and the calendar date it was actually taken on. These
            # differ for anything shot after midnight: a 00:59 photo belongs to the previous
            # evening's story but not to its date, and collapsing the two makes a consumer either
            # mis-sort the item or conclude the timestamp is malformed.
            "calendar_date": (row["taken_local"] or "")[:10] or None,
            "event_id": public_event_id(row["local_date"], row["event_seq"]),
            "location": (
                {
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "source": row["gps_source"],
                    "confidence": row["gps_confidence"],
                    "place": places.get(row["place_id"]),
                }
                if row["lat"] is not None and row["lon"] is not None
                else None
            ),
            "near_home": bool(row["is_near_home"]),
            # Components, not just `overall`: a bare 0.88 tells a reader nothing about why a
            # photo won. Aesthetic and composition scores are deliberately absent -- the
            # pipeline does not compute them, and a plausible invention is worse than a gap.
            "quality": (
                {
                    "overall": row["overall"],
                    "sharpness": row["sharpness"],
                    "exposure": row["exposure"],
                    "contrast": row["contrast"],
                    "face_count": row["face_count"],
                    "face_max_frac": row["face_max_frac"],
                    "content_class": row["content_class"],
                }
                if row["overall"] is not None
                else None
            ),
            "cluster": clusters.get(row["hash"]),
            "selected": selection.get(row["hash"], {}),
            "landmark_ids": landmarks_by_media.get(row["hash"], []),
        }
        asset["thumbnail"], asset["preview"] = _derived_images(
            row["hash"], kind, out_dir, video_meta
        )
        if kind is MediaKind.VIDEO:
            asset["video"] = _video_block(
                row["hash"],
                row["duration"],
                video_meta,
                transcripts,
                row["hash"] in processed_videos,
                config.video.short_clip_max_seconds,
            )
        assets[asset_id] = asset
    return assets


def _event_location(
    points: list[tuple[float, float]], total: int, config: TimelineConfig
) -> dict[str, Any]:
    """Centroid plus the shape of the event, because one averaged point hides the movement."""
    if not points:
        return {
            "centroid": None,
            "first": None,
            "last": None,
            "radius_m": None,
            "gps_coverage": 0.0,
            "path": None,
        }
    centroid = (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )
    span = _haversine_m(points[0], points[-1])
    path = None
    if span >= config.path_min_span_meters:
        simplified = simplify_path(points, config.path_simplify_meters)
        path = [[round(lat, 6), round(lon, 6)] for lat, lon in simplified]
    return {
        "centroid": [round(centroid[0], 6), round(centroid[1], 6)],
        "first": [round(points[0][0], 6), round(points[0][1], 6)],
        "last": [round(points[-1][0], 6), round(points[-1][1], 6)],
        "radius_m": round(max(_haversine_m(centroid, p) for p in points), 1),
        "gps_coverage": round(len(points) / total, 3) if total else 0.0,
        "path": path,
    }


def _build_days(
    conn: sqlite3.Connection, assets: dict[str, dict[str, Any]], config: Config
) -> list[dict[str, Any]]:
    places = _fetch_places(conn)
    landmarks, _ = _fetch_landmarks(conn)
    by_event: dict[str, list[dict]] = {}
    for asset in assets.values():
        if asset["event_id"] is not None:
            by_event.setdefault(asset["event_id"], []).append(asset)
    for members in by_event.values():
        members.sort(key=lambda a: (a["taken_utc"] or "", a["asset_id"]))

    events_by_day: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT e.*, d.local_date FROM event e JOIN day d ON d.id = e.day_id
        ORDER BY d.local_date, e.seq
        """
    ):
        members = by_event.get(public_event_id(row["local_date"], row["seq"]), [])
        # An event whose every member was rejected was never a stop on the trip -- it was two
        # screenshots taken indoors. Dropping it is different from the empty stops the brief
        # deliberately lists, which had real photographs that simply were not selected.
        if not members:
            continue
        points = [(a["location"]["lat"], a["location"]["lon"]) for a in members if a["location"]]
        event_landmarks = sorted({lid for a in members for lid in a["landmark_ids"]})
        highlights = [
            a["asset_id"]
            for a in sorted(
                (a for a in members if str(SelectionScope.EVENT) in a["selected"]),
                key=lambda a: a["selected"][str(SelectionScope.EVENT)]["rank"],
            )
        ]
        events_by_day.setdefault(row["local_date"], []).append(
            {
                "id": public_event_id(row["local_date"], row["seq"]),
                "seq": row["seq"],
                "label": row["label"],
                "start_local": members[0]["taken_local"] if members else None,
                "end_local": members[-1]["taken_local"] if members else None,
                "duration_seconds": (
                    round(m * 60.0)
                    if (
                        m := _minutes_between(
                            members[0]["taken_utc"] if members else None,
                            members[-1]["taken_utc"] if members else None,
                        )
                    )
                    is not None
                    else None
                ),
                "duration_minutes": (
                    round(m, 1)
                    if (
                        m := _minutes_between(
                            members[0]["taken_utc"] if members else None,
                            members[-1]["taken_utc"] if members else None,
                        )
                    )
                    is not None
                    else None
                ),
                "place": places.get(row["place_id"]),
                "location": _event_location(points, len(members), config.timeline),
                "counts": {
                    "media": len(members),
                    "images": sum(1 for a in members if a["kind"] == str(MediaKind.IMAGE)),
                    "videos": sum(1 for a in members if a["kind"] == str(MediaKind.VIDEO)),
                },
                "landmarks": [landmarks[lid] for lid in event_landmarks if lid in landmarks],
                "highlights": highlights,
                "assets": [a["asset_id"] for a in members],
            }
        )

    days = []
    for row in conn.execute("SELECT * FROM day WHERE trip_id = 1 ORDER BY local_date"):
        date = row["local_date"]
        day_events = events_by_day.get(date, [])
        day_assets = [a for a in assets.values() if a["day"] == date]
        highlights = [
            a["asset_id"]
            for a in sorted(
                (a for a in day_assets if str(SelectionScope.DAY) in a["selected"]),
                key=lambda a: a["selected"][str(SelectionScope.DAY)]["rank"],
            )
        ]
        located = [a for a in day_assets if a["location"]]
        located.sort(key=lambda a: (a["taken_utc"] or "", a["asset_id"]))
        raw = [(a["location"]["lat"], a["location"]["lon"]) for a in located]
        zones = Counter(a["timezone"]["name"] for a in day_assets if a["timezone"]["name"])
        days.append(
            {
                "date": date,
                # The zone the day was lived in. A consumer comparing photo times against an
                # itinerary needs this at the day level, not only per asset.
                "timezone": zones.most_common(1)[0][0] if zones else None,
                "events": day_events,
                "counts": {
                    "media": len(day_assets),
                    "images": sum(1 for a in day_assets if a["kind"] == str(MediaKind.IMAGE)),
                    "videos": sum(1 for a in day_assets if a["kind"] == str(MediaKind.VIDEO)),
                    "events": len(day_events),
                },
                "gps_coverage": round(len(located) / len(day_assets), 3) if day_assets else 0.0,
                "path": [
                    [round(lat, 6), round(lon, 6)]
                    for lat, lon in simplify_path(raw, config.timeline.path_simplify_meters)
                ]
                if len(raw) >= 2
                else [],
                "highlights": highlights,
            }
        )
    return days


def _trip_bound(assets: dict[str, dict], which: str, *, utc: bool = False) -> str | None:
    """The trip's first or last timestamp, chosen by UTC and reported in the requested form.

    Read from the assets rather than the `trip` row so the local bounds carry their UTC offsets,
    like every other timestamp in the document. **Ordered by `taken_utc`, never by the local
    string:** comparing offset-bearing strings lexicographically silently gets it wrong the moment
    a trip spans two zones, since `...T09:00+02:00` sorts after `...T08:00+01:00` while being the
    earlier instant.
    """
    dated = [a for a in assets.values() if a["taken_utc"]]
    if not dated:
        return None
    chosen = (min if which == "min" else max)(dated, key=lambda a: a["taken_utc"])
    return chosen["taken_utc"] if utc else chosen["taken_local"]


def _context_block(context: TripContext) -> dict[str, Any]:
    """Trip context, with an explicit `supplied` flag.

    The flag matters more than the fields. A journal written without it is impersonal *because
    nothing was supplied*, and the consumer must be able to say so rather than infer an absence
    of feeling from an absence of data.
    """
    return {
        "supplied": not context.is_empty,
        "journal_voice": context.journal_voice,
        "travelers": [asdict(t) for t in context.travelers],
        "known_plans": list(context.known_plans),
        "notes": list(context.notes),
    }


def build_timeline(
    conn: sqlite3.Connection,
    config: Config,
    context: TripContext | None = None,
    out_dir: Path | None = None,
    overrides: Overrides | None = None,
) -> dict[str, Any]:
    """Assemble the whole document. Reads the DB and probes for derived images; writes nothing."""
    context = context or TripContext()
    rejected = resolve(overrides, conn).reject if overrides else frozenset()
    trip_row = conn.execute("SELECT * FROM trip WHERE id = 1").fetchone()
    assets = _build_assets(conn, config, out_dir, rejected)
    days = _build_days(conn, assets, config)

    images = [a for a in assets.values() if a["kind"] == str(MediaKind.IMAGE)]
    videos = [a for a in assets.values() if a["kind"] == str(MediaKind.VIDEO)]
    undated = [a for a in assets.values() if not a["taken_utc"]]
    trip_highlights = [
        a["asset_id"]
        for a in sorted(
            (a for a in assets.values() if str(SelectionScope.TRIP) in a["selected"]),
            key=lambda a: a["selected"][str(SelectionScope.TRIP)]["rank"],
        )
    ]

    return {
        "schema_version": TRIP_JSON_SCHEMA_VERSION,
        "generator": {"tool": "story-book", "version": __version__},
        "trip": {
            "name": trip_row["name"] if trip_row else None,
            # The rule that decides which trip day an after-midnight photo belongs to. Stated so a
            # consumer does not have to reverse-engineer it from a stop at 00:59 filed under the
            # previous date.
            "day_assignment_rule": (
                f"a trip day runs from {config.time.day_start_hour:02d}:00 local to "
                f"{config.time.day_start_hour:02d}:00 the next morning"
            ),
            "timezone": (
                Counter(
                    a["timezone"]["name"] for a in assets.values() if a["timezone"]["name"]
                ).most_common(1)[0][0]
                if any(a["timezone"]["name"] for a in assets.values())
                else None
            ),
            "start_local": _trip_bound(assets, "min"),
            "end_local": _trip_bound(assets, "max"),
            "start_utc": _trip_bound(assets, "min", utc=True),
            "end_utc": _trip_bound(assets, "max", utc=True),
            "counts": {
                "media": len(assets),
                "images": len(images),
                "videos": len(videos),
                "days": len(days),
                "events": sum(len(d["events"]) for d in days),
                "undated": len(undated),
                "day_highlights": sum(len(d["highlights"]) for d in days),
                "trip_highlights": len(trip_highlights),
            },
        },
        # A filter that did not run must not read as a filter that found nothing.
        "privacy": {
            "home_configured": config.home is not None,
            "exclusion_km": config.home.exclusion_km if config.home else None,
            "excluded_near_home": sum(1 for a in assets.values() if a["near_home"]),
            # Stated, not silent: a total that shrank because a human said so is not the same as
            # a total that was always that size.
            "excluded_by_override": len(rejected),
        },
        "context": _context_block(context),
        "assets": assets,
        "days": days,
        "trip_highlights": trip_highlights,
    }


class TimelineStage(WholeTripStage):
    """Write `trip.json`, the artifact the report and the package both render from."""

    name = "timeline"
    version = 1
    # An aggregate over everything: days, events, selection and landmarks all change underneath
    # it, and a cached `trip.json` would describe a library that no longer exists.
    always_run = True

    def run(self, ctx: StageContext) -> None:
        document = build_timeline(
            ctx.conn, ctx.config, ctx.trip_context, ctx.out_dir, ctx.overrides
        )
        target = ctx.out_dir / TRIP_JSON_FILENAME
        target.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")
        counts = document["trip"]["counts"]
        logger.info(
            "timeline: %s -- %d asset(s), %d day(s), %d event(s), %d highlight(s)",
            target.name,
            counts["media"],
            counts["days"],
            counts["events"],
            counts["day_highlights"],
        )
