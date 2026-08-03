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
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from story_book.config import Config
from story_book.export.fonts import font_identity, load_font, renderable

logger = logging.getLogger(__name__)

REEL_VERSION = 1
"""Bumping this invalidates every cached segment, exactly like a stage `version`."""

REEL_DIRNAME = "reel"
REEL_FILENAME = "trip.mp4"
REEL_JSON_FILENAME = "reel.json"
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


@dataclass(slots=True)
class ReelPlan:
    segments: list[Segment]
    width: int
    height: int
    fps: int
    crossfade: float
    notes: list[str] = field(default_factory=list)
    clips_as_stills: list[str] = field(default_factory=list)
    clips_with_sound: list[str] = field(default_factory=list)
    clips_without_sound: list[str] = field(default_factory=list)

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
        proxies = sorted(package_dir.glob(f"*/video_proxies/{asset_id}.mp4"))
        if proxies:
            sources[asset_id] = ClipSource("proxy", proxies[0])
            continue
        if source_dir is not None:
            matches = sorted(source_dir.rglob(asset["filename"]))
            if len(matches) == 1:
                sources[asset_id] = ClipSource("original", matches[0])
            elif len(matches) > 1:
                logger.warning(
                    "reel: %s matches %d files under %s; skipping rather than guessing",
                    asset["filename"],
                    len(matches),
                    source_dir,
                )
    return sources


def _story_days(story: dict | None) -> dict[str, dict]:
    if not story:
        return {}
    return {d["date"]: d for d in story.get("days", []) if d.get("date")}


def _story_ranges(story: dict | None) -> dict[str, tuple[float, float]]:
    """Source ranges a story named, keyed by asset. Only `video_scenes`, the contract shape."""
    ranges: dict[str, tuple[float, float]] = {}
    for scene in (story or {}).get("video_scenes", []) or []:
        asset_id = scene.get("asset_id")
        start, end = scene.get("source_start_seconds"), scene.get("source_end_seconds")
        usable = isinstance(start, int | float) and isinstance(end, int | float) and end > start
        if asset_id and usable:
            ranges[asset_id] = (float(start), float(end))
    return ranges


def _place_label(asset: dict) -> str | None:
    place = (asset.get("location") or {}).get("place") or {}
    for key in ("poi", "city", "region"):
        if place.get(key):
            return str(place[key])
    return None


def _clip_excerpt(
    asset: dict, config: Config, story_range: tuple[float, float] | None
) -> tuple[float, float, str] | None:
    """`(start, seconds, why)` for a clip, or None if there is not enough footage to use."""
    duration = ((asset.get("video") or {}).get("duration_seconds")) or 0.0
    if duration <= 0:
        return None

    if story_range is not None:
        start = max(0.0, min(story_range[0], duration))
        seconds = min(story_range[1] - story_range[0], duration - start)
        if seconds >= config.reel.clip_min_seconds:
            return start, seconds, "story_range"

    if duration <= config.reel.clip_seconds:
        if duration < config.reel.clip_min_seconds:
            return None
        return 0.0, duration, "whole_clip"

    start = min(duration * POSTER_TIME_FRACTION, POSTER_TIME_MAX_SECONDS)
    seconds = min(config.reel.clip_seconds, duration - start)
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
) -> ReelPlan:
    """Decide what the reel contains. Pure: no filesystem, no ffmpeg, no clock.

    Order is `taken_utc` throughout, never local wall time -- `taken_local` carries an offset and
    mixing the two is how this project broke event durations and trip bounds on separate days.
    """
    width, height = frame_size(config)
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
    days = [d for d in doc.get("days", []) if only_day is None or d["date"] == only_day]
    if only_day is not None and not days:
        raise ReelError(f"no day {only_day} in this trip")

    if only_day is None:
        plan.segments.append(
            Segment(
                kind="title",
                seconds=float(config.reel.seconds_per_title),
                title=(story or {}).get("title") or trip.get("name") or "Trip",
                subtitle=(story or {}).get("subtitle") or _trip_dates(trip),
            )
        )

    for day in days:
        date = day["date"]
        day_story = story_by_day.get(date, {})
        chosen = {a for a in day.get("highlights", []) if a in assets}
        videos = {
            a["asset_id"]
            for a in (assets[i] for i in _day_asset_ids(day, assets))
            if a.get("kind") == "video"
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

    if len(plan.segments) < 2:
        raise ReelError("nothing to render: the trip has no highlights or previews")

    if plan.clips_as_stills:
        plan.notes.append(
            f"{len(plan.clips_as_stills)} clip(s) had no proxy or reachable original and were "
            "rendered as their poster frame. Run `story-book package --video-proxies`, or pass "
            "`--source`, for moving footage."
        )
    return plan


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
    ranges: dict[str, tuple[float, float]],
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
    if segment.kind == "title":
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


def render_title_card(segment: Segment, width: int, height: int, target: Path) -> Path:
    """Draw a title card as a PNG. Pillow's bundled font -- no TTF needs to exist here."""
    image = Image.new("RGB", (width, height), TITLE_BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = load_font(max(28, height // 14))
    subtitle_font = load_font(max(18, height // 30))

    title = renderable(segment.title or "", title_font)
    subtitle = renderable(segment.subtitle or "", subtitle_font)

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
        # `0:a?` is optional on purpose: a silent clip must render, not fail. Whether a segment
        # ended up with sound is then read back off the file rather than assumed.
        audio = (
            ["-map", "0:a?", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
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

    _run_ffmpeg(command, segment.filename or segment.title or segment.kind)
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
) -> RenderedReel:
    """Render every segment (cached), then join them with crossfades and mix any music."""
    if not ffmpeg_available():
        raise ReelError("ffmpeg and ffprobe are required to render a reel")

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

    target = reel_dir / REEL_FILENAME
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

    manifest = write_reel_json(plan, config, reel_dir, music=music, duration=total)
    return RenderedReel(target, manifest, plan, total, rendered, cached)


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
            "file": REEL_FILENAME,
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
                    "start_seconds": round(s.clip_start, 3),
                    "seconds": round(s.seconds, 3),
                    "chosen_by": s.excerpt,
                }
                for s in clips
                if s.asset_id
            },
        },
        "notes": plan.notes,
    }
    target = reel_dir / REEL_JSON_FILENAME
    target.write_text(json.dumps(document, indent=2) + "\n")
    return target
