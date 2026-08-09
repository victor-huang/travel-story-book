"""Reel: a Memories-style video montage rendered from `trip.json`.

Design notes -- the full reasoning lives in `dev_plan/reel_video_montage.md`.

* **This is an export, not a pipeline stage.** Like `report.py` and `package.py` it is a pure
  function of `trip.json` plus derived images, and it never reads the database. That is what
  makes it inherit the home-exclusion filter, the `reject` list and the content-class rules
  instead of reimplementing privacy.
* **Every segment is cached by the hash of its own spec**, never by its position in the list --
  otherwise inserting one photo at the front invalidates the whole reel. Rendering ~60 segments
  is minutes of ffmpeg and an interrupt must not cost all of it.
* **Title cards are drawn with Pillow, not ffmpeg's `drawtext`**, which needs a libfreetype
  build and a font path that exists on the user's machine. `ImageFont.load_default(size=...)`
  is bundled and always present -- the same choice `contact_sheet.py` already made.
* **No excerpt of a long clip is claimed to be its best moment.** `video.motion_score` is
  computed per clip, not per window, so the only honest options are a range a story supplies or
  an arbitrary one. Which was used is recorded per clip in `reel.json`. Automatic ranges are
  P05/Phase 2.
* **No audio ships with this tool.** Music is a path the user passes; nothing is bundled,
  because nothing can be redistributed without a licence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from story_book.config import Config
from story_book.export.fonts import can_render, font_for, font_identity, renderable
from story_book.export.subtitles import (
    SUBTITLE_SCALE_RANGE,
    SubtitleTrack,
    build_cues,
    burn_in,
    cue_font_size,
    mux_subtitles,
    render_cue_images,
    write_track,
)

logger = logging.getLogger(__name__)

REEL_VERSION = 1
"""Bumping this invalidates every cached segment, exactly like a stage `version`."""

REEL_DIRNAME = "reel"
REEL_FILENAME = "trip.mp4"
REEL_JSON_FILENAME = "reel.json"


def reel_filenames(slug: str | None) -> tuple[str, str]:
    """`(video, manifest)` names. Any narrowed render gets its own pair.

    Without this, `--day` writes `trip.mp4` every time and rendering five in a row leaves only the
    fifth -- silently replacing the whole-trip reel on the way.
    """
    if not slug:
        return REEL_FILENAME, REEL_JSON_FILENAME
    return f"trip.{slug}.mp4", f"reel.{slug}.json"


SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text: str) -> str:
    return SLUG_UNSAFE.sub("-", text.strip()).strip("-").lower() or "part"


@dataclass(frozen=True, slots=True)
class ReelSelection:
    """Which slice of the trip a reel covers.

    A 22-day trip is 13 minutes as one montage, so it wants cutting into parts -- and the natural
    seams are geographic, not arithmetic. Dates alone cannot express "the Salzburg leg" when a
    travel day straddles two regions, and places alone cannot separate two visits to the same city.
    Both filters compose with AND so either can carry the work.
    """

    day: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    places: tuple[str, ...] = ()
    """Case-insensitive substrings matched against an asset's poi, city, region or country."""
    name: str | None = None

    @property
    def is_whole_trip(self) -> bool:
        return not (self.day or self.date_from or self.date_to or self.places)

    @property
    def slug(self) -> str | None:
        if self.name:
            return slugify(self.name)
        if self.is_whole_trip:
            return None
        if self.day:
            return self.day
        if self.places:
            return slugify("-".join(self.places))
        return slugify(f"{self.date_from or 'start'}_{self.date_to or 'end'}")

    def covers_day(self, date: str) -> bool:
        if self.day:
            return date == self.day
        if self.date_from and date < self.date_from:
            return False
        return not (self.date_to and date > self.date_to)

    def covers_asset(self, asset: dict) -> bool:
        if not self.places:
            return True
        place = (asset.get("location") or {}).get("place") or {}
        haystack = " ".join(
            str(place.get(key) or "") for key in ("poi", "city", "region", "country")
        ).lower()
        return any(needle.lower() in haystack for needle in self.places)


SEGMENT_CACHE_DIRNAME = ".cache/segments"

FFMPEG_TIMEOUT_SECONDS = 600

TITLE_BACKGROUND = (18, 18, 20)
TITLE_COLOR = (245, 245, 245)
SUBTITLE_COLOR = (170, 170, 175)

POSTER_TIME_FRACTION = 0.1
POSTER_TIME_MAX_SECONDS = 1.0
"""Where a clip excerpt starts, matching `pipeline/video.py`'s poster offset so the clip opens on
the frame the report already shows. Arbitrary, and reported as such."""


class ReelError(Exception):
    """Something the user can fix: a bad aspect string, a missing binary, a failed render."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def parse_aspect(text: str) -> tuple[int, int]:
    """`"16:9"` -> `(16, 9)`. Raises `ReelError` on anything else."""
    parts = str(text).split(":")
    if len(parts) != 2:
        raise ReelError(f"aspect must look like '16:9', got {text!r}")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ReelError(f"aspect must be two whole numbers, got {text!r}") from exc
    if width <= 0 or height <= 0:
        raise ReelError(f"aspect parts must be positive, got {text!r}")
    return width, height


def _even(value: int) -> int:
    """H.264 with yuv420p needs even dimensions on both axes."""
    return value if value % 2 == 0 else value + 1


def frame_size(config: Config) -> tuple[int, int]:
    aspect_w, aspect_h = parse_aspect(config.reel.aspect)
    height = _even(int(config.reel.height))
    width = _even(round(height * aspect_w / aspect_h))
    return width, height


@dataclass(frozen=True, slots=True)
class ClipSource:
    """Where a clip's moving pixels come from, and how good they are."""

    role: str  # "proxy" | "original"
    path: Path
    height: int | None = None
    """Source height in pixels, so a clip rendered below the frame size can be reported."""


@dataclass(frozen=True, slots=True)
class Segment:
    """One shot. Everything here feeds the cache key, so it must fully determine the pixels."""

    kind: str  # "title" | "still" | "clip"
    seconds: float
    asset_id: str | None = None
    filename: str | None = None
    source: str | None = None  # path, relative to `out` for derived files
    source_role: str | None = None  # "preview" | "poster" | "proxy" | "original"
    title: str | None = None
    subtitle: str | None = None
    clip_start: float = 0.0
    excerpt: str | None = None  # "story_range" | "fixed_head" | "whole_clip"
    day: str | None = None
    with_audio: bool = False  # keep this clip's own sound; part of the cache key by design
    source_height: int | None = None
    """Height of the file this clip is cut from, for reporting an enlargement."""
    sources: tuple[str, ...] = ()
    """Images tiled behind an end card. Part of the cache key, so changing the highlights that
    appear in the mosaic rebuilds it."""


@dataclass(slots=True)
class ReelPlan:
    segments: list[Segment]
    width: int
    height: int
    fps: int
    crossfade: float
    notes: list[str] = field(default_factory=list)
    clips_as_stills: list[str] = field(default_factory=list)
    upscaled_clips: list[str] = field(default_factory=list)
    clips_with_sound: list[str] = field(default_factory=list)
    clips_without_sound: list[str] = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)
    burned_in: str | None = None
    slug: str | None = None
    """Set when the render is narrower than the whole trip, so it gets its own filename."""
    clip_timeline_starts: dict[str, float] = field(default_factory=dict)
    """Asset id -> when the clip begins *in the finished reel*, in seconds.

    Distinct from `Segment.clip_start`, which is an offset into the source file. Both are needed
    and neither substitutes: the source offset says which part of the footage was used, this says
    when to go and listen to it."""

    @property
    def duration(self) -> float:
        """Total after crossfades eat `crossfade` seconds from each join."""
        total = sum(s.seconds for s in self.segments)
        return max(0.0, total - self.crossfade * max(0, len(self.segments) - 1))


def resolve_clip_sources(
    doc: dict, out_dir: Path, source_dir: Path | None = None
) -> dict[str, ClipSource]:
    """Find moving pixels for each video, best first: package proxy, then the source tree.

    `trip.json` publishes posters and keyframes but deliberately not source paths -- a published
    artifact must not leak the source tree -- so the reel has to go looking. A clip with neither
    a proxy nor a reachable original is not an error: it becomes its poster frame, and
    `reel.json` says so.
    """
    sources: dict[str, ClipSource] = {}
    package_dir = out_dir / "package"
    for asset_id, asset in doc.get("assets", {}).items():
        if asset.get("kind") != "video":
            continue

        # The original first. A proxy is built to be *small enough to upload to a chat* -- 720p at
        # CRF 28 -- so rendering from one and scaling up to 1080p threw away 59% of the detail on
        # the real trip. It is the fallback, not the preference.
        if source_dir is not None:
            matches = sorted(source_dir.rglob(asset["filename"]))
            if len(matches) == 1:
                sources[asset_id] = ClipSource("original", matches[0], probe_height(matches[0]))
                continue
            if len(matches) > 1:
                logger.warning(
                    "reel: %s matches %d files under %s; skipping rather than guessing",
                    asset["filename"],
                    len(matches),
                    source_dir,
                )

        proxies = sorted(package_dir.glob(f"*/video_proxies/{asset_id}.mp4"))
        if proxies:
            sources[asset_id] = ClipSource("proxy", proxies[0], probe_height(proxies[0]))
    return sources


def probe_height(path: Path) -> int | None:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", str(path),
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        return int(result.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _story_days(story: dict | None) -> dict[str, dict]:
    if not story:
        return {}
    return {d["date"]: d for d in story.get("days", []) if d.get("date")}


@dataclass(frozen=True, slots=True)
class StoryScene:
    """What a story asked for on one clip: where to cut, and how long to run it."""

    start: float
    end: float
    timeline_seconds: float | None
    """`timeline_duration_seconds` -- how long the story wants it *on screen*, which is not the
    same as the length of the source range it chose. Honouring the range and ignoring this made 67
    clips into 12.4 minutes of footage where the story had asked for 6.5."""


def _story_ranges(story: dict | None) -> dict[str, StoryScene]:
    """Source ranges a story named, keyed by asset. Only `video_scenes`, the contract shape."""
    ranges: dict[str, StoryScene] = {}
    for scene in (story or {}).get("video_scenes", []) or []:
        asset_id = scene.get("asset_id")
        start, end = scene.get("source_start_seconds"), scene.get("source_end_seconds")
        usable = isinstance(start, int | float) and isinstance(end, int | float) and end > start
        if asset_id and usable:
            wanted = scene.get("timeline_duration_seconds")
            ranges[asset_id] = StoryScene(
                float(start),
                float(end),
                float(wanted) if isinstance(wanted, int | float) and wanted > 0 else None,
            )
    return ranges


def _place_label(asset: dict) -> str | None:
    place = (asset.get("location") or {}).get("place") or {}
    for key in ("poi", "city", "region"):
        if place.get(key):
            return str(place[key])
    return None


def _clip_excerpt(
    asset: dict, config: Config, story_range: StoryScene | None
) -> tuple[float, float, str] | None:
    """`(start, seconds, why)` for a clip, or None if there is not enough footage to use."""
    duration = ((asset.get("video") or {}).get("duration_seconds")) or 0.0
    if duration <= 0:
        return None

    if story_range is not None:
        start = max(0.0, min(story_range.start, duration))
        # The story's own on-screen duration wins over the length of the range it cut from, and
        # `clip_max_seconds` caps both: how long the montage runs is the renderer's call.
        seconds = min(
            story_range.timeline_seconds or (story_range.end - story_range.start),
            story_range.end - story_range.start,
            duration - start,
            config.reel.clip_max_seconds,
        )
        if seconds >= config.reel.clip_min_seconds:
            return start, seconds, "story_range"

    if duration <= config.reel.clip_seconds:
        if duration < config.reel.clip_min_seconds:
            return None
        return 0.0, duration, "whole_clip"

    start = min(duration * POSTER_TIME_FRACTION, POSTER_TIME_MAX_SECONDS)
    seconds = min(config.reel.clip_seconds, duration - start, config.reel.clip_max_seconds)
    if seconds < config.reel.clip_min_seconds:
        return None
    return start, seconds, "fixed_head"


def build_plan(
    doc: dict,
    config: Config,
    *,
    story: dict | None = None,
    clip_sources: dict[str, ClipSource] | None = None,
    only_day: str | None = None,
    selection: ReelSelection | None = None,
) -> ReelPlan:
    """Decide what the reel contains. Pure: no filesystem, no ffmpeg, no clock.

    Order is `taken_utc` throughout, never local wall time -- `taken_local` carries an offset and
    mixing the two is how this project broke event durations and trip bounds on separate days.
    """
    width, height = frame_size(config)
    selection = selection or ReelSelection(day=only_day)
    clip_sources = clip_sources or {}
    assets = doc.get("assets", {})
    story_by_day = _story_days(story)
    ranges = _story_ranges(story)

    plan = ReelPlan(
        segments=[],
        width=width,
        height=height,
        fps=int(config.reel.fps),
        crossfade=float(config.reel.crossfade_seconds),
    )

    trip = doc.get("trip", {})
    days = [d for d in doc.get("days", []) if selection.covers_day(d["date"])]
    if selection.day and not days:
        raise ReelError(f"no day {selection.day} in this trip")
    if not days:
        raise ReelError(
            f"no day between {selection.date_from or 'the start'} and "
            f"{selection.date_to or 'the end'} of this trip"
        )
    plan.slug = selection.slug

    # A narrowed reel still opens on a title card; only a single-day render skips it, because the
    # day's own card follows immediately.
    if not selection.day:
        plan.segments.append(
            Segment(
                kind="title",
                seconds=float(config.reel.seconds_per_title),
                title=selection.name or (story or {}).get("title") or trip.get("name") or "Trip",
                subtitle=(story or {}).get("subtitle") or _trip_dates(trip),
            )
        )

    for day in days:
        date = day["date"]
        day_story = story_by_day.get(date, {})
        chosen = {
            a
            for a in day.get("highlights", [])
            if a in assets and selection.covers_asset(assets[a])
        }
        videos = {
            a["asset_id"]
            for a in (assets[i] for i in _day_asset_ids(day, assets))
            if a.get("kind") == "video" and selection.covers_asset(a)
        }
        members = sorted(chosen | videos, key=lambda i: (assets[i].get("taken_utc") or "", i))
        if not members:
            continue

        first = assets[members[0]]
        plan.segments.append(
            Segment(
                kind="title",
                seconds=float(config.reel.seconds_per_title),
                title=day_story.get("title") or _place_label(first) or date,
                subtitle=date,
                day=date,
            )
        )

        for asset_id in members:
            asset = assets[asset_id]
            segment = (
                _clip_segment(asset, config, clip_sources, ranges, plan)
                if asset.get("kind") == "video"
                else _still_segment(asset, config)
            )
            if segment is not None:
                plan.segments.append(segment)

    if config.reel.end_card and len(plan.segments) > 1:
        plan.segments.append(_end_card(plan, config, selection, story, trip))

    if len(plan.segments) < 2:
        raise ReelError(
            "nothing to render: no highlights or previews match this selection"
            + (f" (places: {', '.join(selection.places)})" if selection.places else "")
        )

    small = [
        s
        for s in plan.segments
        if s.kind == "clip" and s.source_height and s.source_height < plan.height
    ]
    upscaled = sorted({s.filename or s.asset_id or "?" for s in small})
    if upscaled:
        plan.upscaled_clips = upscaled
        # Two different situations wear the same symptom, and the advice differs. Telling
        # someone to pass `--source` when they already did, and the original is simply a 720p
        # download, sends them to fix something that is not broken.
        from_proxy = any(s.source_role == "proxy" for s in small)
        remedy = (
            "Pass --source <folder> to render from the originals; a package proxy is built "
            "small enough to upload, not to render from."
            if from_proxy
            else "These are the originals -- the footage itself is below the frame height, so "
            "there is nothing sharper to render from."
        )
        plan.notes.append(
            f"{len(upscaled)} clip(s) were enlarged to fit the frame -- their source is shorter "
            f"than {plan.height}px. {remedy}"
        )

    if plan.clips_as_stills:
        plan.notes.append(
            f"{len(plan.clips_as_stills)} clip(s) had no proxy or reachable original and were "
            "rendered as their poster frame. Run `story-book package --video-proxies`, or pass "
            "`--source`, for moving footage."
        )
    return plan


def _end_card(
    plan: ReelPlan, config: Config, selection: ReelSelection, story: dict | None, trip: dict
) -> Segment:
    """A closing card tiled with stills already in this reel, sampled evenly across it.

    Evenly rather than "the best": the mosaic is a reminder of where the film went, so it should
    span the whole of it rather than cluster on whichever day scored highest.
    """
    stills = [s for s in plan.segments if s.kind == "still" and s.source]
    columns, rows = mosaic_grid(
        min(len(stills), max(1, int(config.reel.end_card_tiles))), plan.width, plan.height
    )
    wanted = columns * rows
    if len(stills) > wanted:
        step = len(stills) / wanted
        stills = [stills[int(index * step)] for index in range(wanted)]
    return Segment(
        kind="end",
        seconds=float(config.reel.end_card_seconds),
        title=config.reel.end_card_text,
        subtitle=selection.name or (story or {}).get("title") or trip.get("name"),
        sources=tuple(s.source for s in stills if s.source),
    )


def _trip_dates(trip: dict) -> str | None:
    start, end = (trip.get("start_local") or "")[:10], (trip.get("end_local") or "")[:10]
    if not start:
        return None
    return start if start == end else f"{start} – {end}"


def _day_asset_ids(day: dict, assets: dict) -> list[str]:
    ids: list[str] = []
    for event in day.get("events", []):
        ids.extend(a for a in event.get("assets", []) if a in assets)
    return ids


def _still_segment(asset: dict, config: Config) -> Segment | None:
    source = asset.get("preview") or asset.get("thumbnail")
    if not source:
        return None
    return Segment(
        kind="still",
        seconds=float(config.reel.seconds_per_still),
        asset_id=asset["asset_id"],
        filename=asset.get("filename"),
        source=source,
        source_role="preview" if asset.get("preview") else "thumbnail",
        day=asset.get("day"),
    )


def _clip_segment(
    asset: dict,
    config: Config,
    clip_sources: dict[str, ClipSource],
    ranges: dict[str, StoryScene],
    plan: ReelPlan,
) -> Segment | None:
    asset_id = asset["asset_id"]
    source = clip_sources.get(asset_id)
    excerpt = _clip_excerpt(asset, config, ranges.get(asset_id))

    if source is None or excerpt is None:
        # Either we have no moving pixels, or the clip is too short to be worth cutting to.
        # Both mean the same thing to a viewer: it becomes a photograph.
        poster = (asset.get("video") or {}).get("poster") or asset.get("thumbnail")
        if not poster:
            return None
        if source is None:
            plan.clips_as_stills.append(asset.get("filename") or asset_id)
        return Segment(
            kind="still",
            seconds=float(config.reel.seconds_per_still),
            asset_id=asset_id,
            filename=asset.get("filename"),
            source=poster,
            source_role="poster",
            day=asset.get("day"),
        )

    start, seconds, why = excerpt
    return Segment(
        kind="clip",
        seconds=seconds,
        asset_id=asset_id,
        filename=asset.get("filename"),
        source=str(source.path),
        source_role=source.role,
        clip_start=start,
        excerpt=why,
        day=asset.get("day"),
        with_audio=bool(config.reel.clip_audio),
        source_height=source.height,
    )


def segment_key(segment: Segment, plan: ReelPlan, config: Config) -> str:
    """Content hash of everything that determines this segment's pixels.

    Deliberately excludes the segment's index: inserting a photo at the front of the reel must
    not invalidate every segment behind it.
    """
    payload = {
        "reel_version": REEL_VERSION,
        "segment": asdict(segment),
        "frame": [plan.width, plan.height, plan.fps],
        "encode": [config.reel.x264_preset, config.reel.x264_crf],
    }
    if segment.kind in ("title", "end"):
        payload["font"] = font_identity()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def _fill_filter(width: int, height: int) -> str:
    """Scale to fit, over a blurred copy scaled to cover. Nothing is ever cropped away.

    The library is 54% landscape and 46% portrait, so roughly half of any montage is off-aspect
    whichever frame is chosen. Cropping toward the subject would need face bounding boxes, which
    the schema does not store -- only `face_count` and `face_max_frac`.
    """
    small_w, small_h = _even(max(2, width // 8)), _even(max(2, height // 8))
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},scale={small_w}:{small_h},gblur=sigma=6,"
        f"scale={width}:{height},eq=brightness=-0.10[bgb];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,"
        f"setsar=1,format=yuv420p[v]"
    )


def _draw_card_text(
    image: Image.Image, title: str | None, subtitle: str | None, width: int, height: int
) -> None:
    """Centred title over optional subtitle, in a font chosen by what has to be drawn."""
    draw = ImageDraw.Draw(image)

    # Chosen by what has to be drawn, not by a fixed default -- a Chinese day title needs a CJK
    # font, and Arial would silently render it as nothing at all.
    title_font = font_for(title or "", max(28, height // 14))
    subtitle_font = font_for(subtitle or "", max(18, height // 30))

    title = renderable(title or "", title_font)
    subtitle = renderable(subtitle or "", subtitle_font)

    lines = _wrap(draw, title, title_font, int(width * 0.82))
    line_height = int(title_font.size * 1.25)
    block = line_height * len(lines)
    subtitle_gap = int(subtitle_font.size * 1.8) if subtitle else 0
    top = (height - block - subtitle_gap) // 2

    for index, line in enumerate(lines):
        span = draw.textlength(line, font=title_font)
        draw.text(
            ((width - span) / 2, top + index * line_height), line, font=title_font, fill=TITLE_COLOR
        )
    if subtitle:
        span = draw.textlength(subtitle, font=subtitle_font)
        draw.text(
            ((width - span) / 2, top + block + subtitle_gap // 2),
            subtitle,
            font=subtitle_font,
            fill=SUBTITLE_COLOR,
        )


def render_title_card(segment: Segment, width: int, height: int, target: Path) -> Path:
    """Draw a title card as a PNG. The font is chosen by what has to be drawn -- see `fonts.py`."""
    image = Image.new("RGB", (width, height), TITLE_BACKGROUND)
    _draw_card_text(image, segment.title, segment.subtitle, width, height)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return target


def mosaic_grid(tiles: int, width: int, height: int) -> tuple[int, int]:
    """Columns and rows that use `tiles` exactly and sit closest to the frame's shape.

    Exactly, not at most: a 12-tile mosaic laid out five wide leaves three empty cells in the
    corner, and a hole in a mosaic reads as a rendering failure rather than a design.
    """
    if tiles <= 1:
        return 1, 1
    target = width / max(1, height)
    best, best_error = (tiles, 1), None
    for columns in range(1, tiles + 1):
        for rows in range(1, tiles // columns + 1):
            used = columns * rows
            if used > tiles:
                continue
            # Shape first, then a mild preference for leaving no photograph out.
            error = abs(columns / rows - target) + (tiles - used) * 0.08
            if best_error is None or error < best_error:
                best, best_error = (columns, rows), error
    return best


def render_end_card(segment: Segment, width: int, height: int, target: Path, out_dir: Path) -> Path:
    """A mosaic of this reel's own stills, dimmed, with the closing line over it.

    Its own stills rather than a fixed graphic: the card should be of *this* trip, and the images
    are already on disk, so it costs nothing to make it specific.
    """
    image = Image.new("RGB", (width, height), TITLE_BACKGROUND)
    tiles = [out_dir / src for src in segment.sources]
    tiles = [t for t in tiles if t.exists()]

    if tiles:
        columns, rows = mosaic_grid(len(tiles), width, height)
        tiles = tiles[: columns * rows]
        cell_w, cell_h = math.ceil(width / columns), math.ceil(height / rows)
        for index, path in enumerate(tiles):
            try:
                with Image.open(path) as raw:
                    tile = ImageOps.fit(raw.convert("RGB"), (cell_w, cell_h), Image.LANCZOS)
            except OSError:  # an unreadable preview costs one tile, not the card
                continue
            image.paste(tile, ((index % columns) * cell_w, (index // columns) * cell_h))
        # Dim it hard: the mosaic is texture behind the words, not a photograph to read.
        image = Image.blend(image, Image.new("RGB", (width, height), TITLE_BACKGROUND), 0.62)

    _draw_card_text(image, segment.title, segment.subtitle, width, height)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return target


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int):
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _run_ffmpeg(command: list[str], what: str) -> None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReelError(f"ffmpeg failed on {what}: {exc}") from exc
    if result.returncode != 0:
        raise ReelError(f"ffmpeg failed on {what}: {result.stderr[-500:]}")


def probe_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def has_audio_stream(path: Path) -> bool:
    """Whether a rendered segment actually carries sound.

    Read off the file rather than inferred from the plan: `with_audio` is a request, and a clip
    shot on a muted phone grants it no sound. Referencing a stream that is not there is an
    ffmpeg error, so the mix has to know which segments really have one.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "audio" in result.stdout


def _segment_offsets(durations: list[float], crossfade: float) -> list[float]:
    """When each segment starts on the finished timeline, given that crossfades overlap them.

    The same accumulation `_xfade_chain` uses, so clip audio lands exactly where its picture does
    instead of drifting by one crossfade per clip.
    """
    offsets, running = [], 0.0
    for index, duration in enumerate(durations):
        offsets.append(running)
        running += duration - (crossfade if index < len(durations) - 1 else 0.0)
    return offsets


def _audio_graph(
    plan: ReelPlan,
    config: Config,
    durations: list[float],
    audible: list[int],
    music_input: int | None,
    total: float,
) -> tuple[list[str], str] | None:
    """Filter chain for the soundtrack, and the label carrying it. None if there is no sound.

    Two buses. The clip bus is every clip's own audio, delayed to its position on the timeline.
    The music bus is the supplied track, looped and trimmed to length. Where both exist the clip
    bus is *also* used as a sidechain key, so the music ducks under a clip and recovers after it
    -- driven by the actual sound rather than by nominal segment boundaries.
    """
    if not audible and music_input is None:
        return None

    reel = config.reel
    parts: list[str] = []
    offsets = _segment_offsets(durations, plan.crossfade)

    clip_bus: str | None = None
    if audible:
        for index in audible:
            delay_ms = int(round(offsets[index] * 1000))
            parts.append(
                f"[{index}:a]aresample=48000,adelay={delay_ms}:all=1,"
                f"volume={reel.clip_volume}[ca{index}]"
            )
        # A silent bed of the full length keeps amix from ending at the last clip, and gives the
        # music something to mix against when a clip is the only other source.
        parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[abed]")
        inputs = "".join(f"[ca{i}]" for i in audible) + "[abed]"
        parts.append(
            f"{inputs}amix=inputs={len(audible) + 1}:normalize=0:dropout_transition=0[clipbus]"
        )
        clip_bus = "[clipbus]"

    if music_input is None:
        return parts, clip_bus  # type: ignore[return-value]

    fade_at = max(0.0, total - reel.music_fade_seconds)
    parts.append(
        f"[{music_input}:a]aresample=48000,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
        f"volume={reel.music_volume},"
        f"afade=t=out:st={fade_at:.3f}:d={reel.music_fade_seconds}[music]"
    )
    if clip_bus is None:
        return parts, "[music]"

    parts.append(f"{clip_bus}asplit=2[duckkey][clipout]")
    parts.append(
        f"[music][duckkey]sidechaincompress="
        f"threshold={reel.music_duck_threshold}:ratio={reel.music_duck_ratio}:"
        f"attack={reel.music_duck_attack_ms}:release={reel.music_duck_release_ms}[ducked]"
    )
    parts.append("[ducked][clipout]amix=inputs=2:normalize=0:dropout_transition=0[aout]")
    return parts, "[aout]"


def render_segment(
    segment: Segment, plan: ReelPlan, config: Config, out_dir: Path, target: Path
) -> Path:
    """Render one segment to `target`. Returns it untouched if the cache already has it."""
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial.mp4")
    # An interrupted run leaves a half-written partial behind. It is never valid, and leaving it
    # invites ffmpeg or a later reader to treat it as real.
    partial.unlink(missing_ok=True)

    encode = [
        "-c:v",
        "libx264",
        "-preset",
        config.reel.x264_preset,
        "-crf",
        str(config.reel.x264_crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(plan.fps),
    ]

    if segment.kind == "clip":
        source = Path(segment.source or "")
        # `0:a:0?` -- the *first* audio stream, optionally. Two details, both learned from real
        # footage: the `?` lets a silent clip render instead of failing, and the `:0` takes one
        # track rather than all of them. A modern iPhone writes a second `apac` (spatial audio)
        # stream that this ffmpeg has no decoder for, and `0:a?` maps it too, so 58 of 69 clips on
        # the real trip failed with "no decoder found for: none". Proxies hid it, because
        # transcoding one picks a single stream by default.
        audio = (
            ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
            if segment.with_audio
            else ["-an"]
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{segment.clip_start:.3f}",
            "-t", f"{segment.seconds:.3f}",
            "-i", str(source),
            "-filter_complex", _fill_filter(plan.width, plan.height),
            "-map", "[v]",
            *audio,
            *encode,
            str(partial),
        ]  # fmt: skip
    else:
        if segment.kind == "title":
            still = target.with_suffix(".png")
            render_title_card(segment, plan.width, plan.height, still)
        elif segment.kind == "end":
            still = target.with_suffix(".png")
            render_end_card(segment, plan.width, plan.height, still, out_dir)
        else:
            still = out_dir / (segment.source or "")
            if not still.exists():
                what = segment.filename or segment.asset_id
                raise ReelError(f"missing image for {what}: {still}")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1",
            "-framerate", str(plan.fps),
            "-t", f"{segment.seconds:.3f}",
            "-i", str(still),
            "-filter_complex", _fill_filter(plan.width, plan.height),
            "-map", "[v]",
            "-an",
            *encode,
            str(partial),
        ]  # fmt: skip

    what = segment.filename or segment.title or segment.kind
    _run_ffmpeg(command, what)
    if not partial.exists():
        # ffmpeg can exit 0 and still write nothing -- a filter that yields no frames reports a
        # warning, not an error. Checking the exit code alone let that surface as a bare
        # FileNotFoundError from the rename, three frames of stack away from the cause.
        raise ReelError(f"ffmpeg reported success but produced no output for {what}")
    partial.replace(target)  # atomic: a killed render never leaves a valid-looking cache entry
    return target


def _xfade_chain(durations: list[float], crossfade: float) -> tuple[str, str]:
    """The filter chain joining N rendered segments, and the label carrying the result."""
    if len(durations) == 1:
        return "[0:v]null[vout]", "[vout]"
    parts, offset, label = [], 0.0, "[0:v]"
    for index in range(1, len(durations)):
        offset += durations[index - 1] - crossfade
        out = f"[x{index}]"
        parts.append(
            f"{label}[{index}:v]xfade=transition=fade:"
            f"duration={crossfade:.3f}:offset={max(0.0, offset):.3f}{out}"
        )
        label = out
    parts.append(f"{label}null[vout]")
    return ";".join(parts), "[vout]"


@dataclass(slots=True)
class RenderedReel:
    path: Path
    manifest: Path
    plan: ReelPlan
    duration: float
    segments_rendered: int
    segments_cached: int


def render_reel(
    plan: ReelPlan,
    config: Config,
    out_dir: Path,
    *,
    music: Path | None = None,
    progress: Any = None,
    story: dict | None = None,
    subtitle_languages: Sequence[str] = (),
    burn_in_language: str | None = None,
) -> RenderedReel:
    """Render every segment (cached), then join them with crossfades and mix any music."""
    if not ffmpeg_available():
        raise ReelError("ffmpeg and ffprobe are required to render a reel")
    if burn_in_language:
        # Checked before any encoding: a bad value should not cost a full render first.
        low, high = SUBTITLE_SCALE_RANGE
        if not low <= float(config.reel.subtitle_scale) <= high:
            raise ReelError(
                f"reel.subtitle_scale must be between {low} and {high}, "
                f"got {config.reel.subtitle_scale}"
            )

    reel_dir = out_dir / REEL_DIRNAME
    cache_dir = reel_dir / SEGMENT_CACHE_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)

    rendered, cached, paths, durations, audible = 0, 0, [], [], []
    for index, segment in enumerate(plan.segments):
        target = cache_dir / f"{segment_key(segment, plan, config)}.mp4"
        existed = target.exists()
        render_segment(segment, plan, config, out_dir, target)
        cached, rendered = (cached + 1, rendered) if existed else (cached, rendered + 1)
        paths.append(target)
        durations.append(probe_duration(target) or segment.seconds)
        if segment.with_audio and has_audio_stream(target):
            audible.append(index)
        if progress is not None:
            progress(segment, existed)

    plan.clips_with_sound = [
        plan.segments[i].filename or plan.segments[i].asset_id or "?" for i in audible
    ]
    plan.clips_without_sound = [
        s.filename or s.asset_id or "?"
        for i, s in enumerate(plan.segments)
        if s.kind == "clip" and s.with_audio and i not in audible
    ]

    chain, label = _xfade_chain(durations, plan.crossfade)
    total = sum(durations) - plan.crossfade * max(0, len(durations) - 1)

    offsets = _segment_offsets(durations, plan.crossfade)
    plan.clip_timeline_starts = {
        s.asset_id: round(offsets[i], 3)
        for i, s in enumerate(plan.segments)
        if s.kind == "clip" and s.asset_id
    }

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in paths:
        command += ["-i", str(path)]

    music_input = None
    if music is not None:
        music_input = len(paths)
        command += ["-stream_loop", "-1", "-i", str(music)]

    filters = [chain]
    maps = ["-map", label]
    audio = _audio_graph(plan, config, durations, audible, music_input, total)
    if audio is not None:
        audio_parts, audio_label = audio
        filters.extend(audio_parts)
        maps += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]

    video_name, _ = reel_filenames(plan.slug)
    target = reel_dir / video_name
    command += [
        "-filter_complex", ";".join(filters),
        *maps,
        "-c:v", "libx264",
        "-preset", config.reel.x264_preset,
        "-crf", str(config.reel.x264_crf),
        "-pix_fmt", "yuv420p",
        "-r", str(plan.fps),
        "-movflags", "+faststart",
        str(target),
    ]  # fmt: skip
    _run_ffmpeg(command, "final assembly")

    plan.subtitle_tracks = _write_subtitles(
        plan, reel_dir, target, offsets, story, subtitle_languages
    )
    if burn_in_language:
        plan.burned_in = _burn_in_track(plan, config, reel_dir, target, burn_in_language)

    manifest = write_reel_json(plan, config, reel_dir, music=music, duration=total)
    return RenderedReel(target, manifest, plan, total, rendered, cached)


def _burn_in_track(
    plan: ReelPlan, config: Config, reel_dir: Path, video: Path, language: str
) -> str | None:
    """Write a second video with `language` drawn into the frames. Returns its filename.

    A separate file: burn-in re-encodes and cannot be undone, so the clean reel stays as it is.
    """
    track = next((t for t in plan.subtitle_tracks if t.language == language), None)
    if track is None:
        plan.notes.append(
            f"cannot burn in '{language}': no subtitle track was written for it. Add it to "
            "--subtitles, and make sure story.json carries its translations."
        )
        return None

    joined = " ".join(c.text for c in track.cues)
    if not can_render(joined):
        plan.notes.append(
            f"cannot burn in '{language}': no font on this machine can draw its characters, and "
            "drawing blanks would be worse than not drawing. The soft track still works. On Linux "
            "install Noto Sans CJK; the .vtt beside the video needs no font at all."
        )
        return None

    scale = float(config.reel.subtitle_scale)
    cues_dir = reel_dir / SEGMENT_CACHE_DIRNAME.replace("segments", f"cues-{language}")
    images = render_cue_images(
        track,
        plan.width,
        plan.height,
        cues_dir,
        scale=scale,
        bottom_margin=float(config.reel.subtitle_bottom_margin),
    )
    target = reel_dir / f"{video.stem}.{language}.mp4"
    if not burn_in(video, images, target, preset=config.reel.x264_preset, crf=config.reel.x264_crf):
        plan.notes.append(f"burn-in for '{language}' failed; the soft track is unaffected.")
        return None
    return target.name


def _write_subtitles(
    plan: ReelPlan,
    reel_dir: Path,
    video: Path,
    offsets: list[float],
    story: dict | None,
    languages: Sequence[str],
) -> list[SubtitleTrack]:
    """Write a `.vtt` per language and mux them in as selectable tracks.

    A language the story carries no translation for gets **no track at all**, only a warning. A
    Chinese track holding English text would be the artifact claiming something it cannot back up.
    """
    if not languages:
        return []

    written: list[tuple[str, Path]] = []
    tracks: list[SubtitleTrack] = []
    for language in languages:
        track = build_cues(plan.segments, offsets, story, language)
        if not track.cues:
            plan.notes.append(f"no subtitle text found for '{language}'; no track written.")
            continue
        if track.translated_count == 0:
            plan.notes.append(
                f"story.json carries no '{language}' translations, so no '{language}' track was "
                "written -- a track in one language holding another's text would be a lie. Ask "
                "the chat for translations (see the story schema's `translations` field)."
            )
            continue
        if not track.fully_translated:
            missing = len(track.cues) - track.translated_count
            plan.notes.append(
                f"{missing} of {len(track.cues)} '{language}' cues have no translation and show "
                "the original text."
            )
        written.append((language, write_track(track, reel_dir, video.stem)))
        tracks.append(track)

    if written and not mux_subtitles(video, written):
        plan.notes.append(
            "could not mux subtitles into the video; the .vtt files beside it still work."
        )
    return tracks


def write_reel_json(
    plan: ReelPlan, config: Config, reel_dir: Path, *, music: Path | None, duration: float
) -> Path:
    """What was rendered and what was assumed.

    An artifact never overstates its contents. Where the reel cannot supply what a viewer would
    assume -- a beat-matched cut, the best moment of a clip, real footage rather than a poster --
    it says so here rather than letting the output imply otherwise.
    """
    clips = [s for s in plan.segments if s.kind == "clip"]
    document = {
        "reel_version": REEL_VERSION,
        "generator": f"story-book reel (reel_version {REEL_VERSION})",
        "video": {
            "file": reel_filenames(plan.slug)[0],
            "width": plan.width,
            "height": plan.height,
            "aspect": config.reel.aspect,
            "fps": plan.fps,
            "duration_seconds": round(duration, 3),
            "segments": len(plan.segments),
        },
        "audio": {
            "music_supplied": music is not None,
            "music_filename": music.name if music else None,
            "music_volume": config.reel.music_volume if music else None,
            "beat_aligned": False,
            "cut_timing": "fixed cadence -- onset detection is not implemented (T51)",
            "clip_audio_included": bool(plan.clips_with_sound),
            "clips_with_sound": plan.clips_with_sound,
            "clips_with_no_audio_track": plan.clips_without_sound,
            "music_ducked_under_clips": bool(plan.clips_with_sound) and music is not None,
            "ducking": (
                {
                    "method": "sidechaincompress keyed on the clip audio bus",
                    "ratio": config.reel.music_duck_ratio,
                    "threshold": config.reel.music_duck_threshold,
                    "attack_ms": config.reel.music_duck_attack_ms,
                    "release_ms": config.reel.music_duck_release_ms,
                }
                if plan.clips_with_sound and music is not None
                else None
            ),
        },
        "video_sources": {
            "clips_with_footage": len(clips),
            "clips_rendered_as_stills": plan.clips_as_stills,
            "clips_enlarged_to_fit_frame": plan.upscaled_clips,
            "roles": sorted({s.source_role for s in clips if s.source_role}),
        },
        "excerpts": {
            "note": (
                "motion_score is computed per clip, not per window, so no excerpt is claimed to "
                "be a clip's best moment. Automatic ranges are P05/Phase 2."
            ),
            "by_asset": {
                s.asset_id: {
                    "filename": s.filename,
                    "source_start_seconds": round(s.clip_start, 3),
                    "seconds": round(s.seconds, 3),
                    "chosen_by": s.excerpt,
                    # Where to go and listen. The source offset above says which part of the
                    # footage was used; this says when it happens in the reel.
                    "timeline_start_seconds": plan.clip_timeline_starts.get(s.asset_id),
                }
                for s in clips
                if s.asset_id
            },
        },
        "subtitles": {
            "tracks": [
                {
                    "language": t.language,
                    "file": f"{Path(reel_filenames(plan.slug)[0]).stem}.{t.language}.vtt",
                    "cues": len(t.cues),
                    "translated_cues": t.translated_count,
                    "fully_translated": t.fully_translated,
                }
                for t in plan.subtitle_tracks
            ],
            "burned_in_file": plan.burned_in,
            "burned_in_font_px": (
                cue_font_size(plan.height, config.reel.subtitle_scale) if plan.burned_in else None
            ),
            "burned_in_scale": config.reel.subtitle_scale if plan.burned_in else None,
            "note": (
                "A language with no translations in story.json gets no track: a track labelled "
                "one language while holding another's text would misrepresent itself."
            ),
        },
        "notes": plan.notes,
    }
    _, manifest_name = reel_filenames(plan.slug)
    target = reel_dir / manifest_name
    target.write_text(json.dumps(document, indent=2) + "\n")
    return target
