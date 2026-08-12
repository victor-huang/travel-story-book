"""S07: what a reel job's `options` mean, and how the worker measures progress against them.

Shared between `worker.py` (which runs `story-book reel` as a subprocess) and `jobs.py`/
`delivery.py` (which report on it), so the two sides cannot silently disagree about what a
client's request produces on disk -- the exact failure mode a "test the seam" reading of this
project's own retros warns about.

**The worker runs the existing CLI; nothing here reimplements reel rendering.** `reel_argv`
only assembles the same flags `story-book reel --help` documents. The one piece of real logic is
`reel_progress_seed`, and even that calls straight into `story_book.export.reel.build_plan` --
"pure: no filesystem, no ffmpeg, no clock" by that module's own docstring -- so measuring the
render's own plan before it starts is cheap and exact, not an estimate.

**Filenames are computed, never invented.** `reel_selection(options).slug` is the identical pure
function `story-book reel` itself uses to name `trip.mp4` vs `trip.<slug>.mp4`, so this module and
the CLI can never compute two different names for the same request.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from story_book.config import Config, ConfigError
from story_book.export.reel import (
    REEL_DIRNAME,
    SEGMENT_CACHE_DIRNAME,
    ReelError,
    ReelSelection,
    build_plan,
    reel_filenames,
    resolve_clip_sources,
    segment_key,
)
from story_book.export.report import load_story, load_trip_json
from storybook_service.source import TripPaths

STORY_JSON_RELPATH = "story/story.json"
"""Where a model's answer would live, if one existed (`cli.py`'s `STORY_DIRNAME`). D2 defers
trip context collection for the hosted app, so this is normally absent -- `--subtitles` and
`--burn-in` need it, and `story-book reel` itself is what refuses the request when it is missing,
in its own words, quoted from the CLI's exit rather than re-validated here."""


def reel_selection(options: dict[str, Any]) -> ReelSelection:
    """The pure object both this module and `story-book reel` derive a slug from."""
    return ReelSelection(
        day=options.get("day"),
        date_from=options.get("date_from"),
        date_to=options.get("date_to"),
        places=tuple(options.get("places") or ()),
        name=options.get("name"),
    )


def reel_output_paths(paths: TripPaths, options: dict[str, Any]) -> tuple[Path, Path]:
    """`(video_path, manifest_path)` this render will produce -- computed, not guessed."""
    video_name, manifest_name = reel_filenames(reel_selection(options).slug)
    reel_dir = paths.out / REEL_DIRNAME
    return reel_dir / video_name, reel_dir / manifest_name


def _reel_config(paths: TripPaths, options: dict[str, Any]) -> Config:
    try:
        config = Config.load(paths.config) if paths.config.exists() else Config()
    except ConfigError:
        config = Config()
    changes: dict[str, Any] = {}
    if options.get("aspect"):
        changes["aspect"] = options["aspect"]
    if options.get("clip_audio") is not None:
        changes["clip_audio"] = options["clip_audio"]
    if changes:
        config = replace(config, reel=replace(config.reel, **changes))
    return config


def reel_argv(
    story_book_bin: str, paths: TripPaths, options: dict[str, Any], music_path: Path | None
) -> list[str]:
    """The same flags a person would type, assembled from the client's declared `options`."""
    argv = [story_book_bin, "reel", "--out", str(paths.out)]
    if paths.config.exists():
        argv += ["--config", str(paths.config)]
    if paths.source.is_dir():
        # Best quality first (dev_plan/reel_video_montage.md: "the original comes first, not the
        # proxy"). No `--video-proxies` package exists on this deployment yet, so the materialised
        # source tree -- already fetched by `_prepare` for the build this reel depends on -- is the
        # only source of moving pixels; `resolve_clip_sources` falls back to poster stills if a
        # clip's file cannot be found under it, never to an error.
        argv += ["--source", str(paths.source)]
    if music_path is not None:
        argv += ["--music", str(music_path)]
    if options.get("aspect"):
        argv += ["--aspect", options["aspect"]]
    if options.get("clip_audio") is not None:
        argv.append("--clip-audio" if options["clip_audio"] else "--no-clip-audio")
    if options.get("day"):
        argv += ["--day", options["day"]]
    if options.get("date_from"):
        argv += ["--from", options["date_from"]]
    if options.get("date_to"):
        argv += ["--to", options["date_to"]]
    if options.get("places"):
        argv += ["--place", ",".join(options["places"])]
    if options.get("name"):
        argv += ["--name", options["name"]]
    if options.get("subtitles"):
        argv += ["--subtitles", ",".join(options["subtitles"])]
    if options.get("burn_in"):
        argv += ["--burn-in", options["burn_in"]]
    return argv


def reel_progress_seed(*, paths: TripPaths, options: dict[str, Any]) -> dict[str, Any] | None:
    """The exact segment plan this render will use, computed once before rendering starts.

    `None` means the plan could not be computed here (a bad day/aspect, `trip.json` missing, a
    story required for subtitles but absent). That is never treated as a job failure -- the
    render itself is the actual source of truth for success or failure, and this is best-effort
    progress enrichment layered on top of it, not a gate in front of it.
    """
    trip_json_path = paths.out / "trip.json"
    if not trip_json_path.is_file():
        return None
    try:
        document = load_trip_json(trip_json_path)
        config = _reel_config(paths, options)
        story_path = paths.out / STORY_JSON_RELPATH
        story = load_story(story_path) if story_path.is_file() else None
        selection = reel_selection(options)
        source_dir = paths.source if paths.source.is_dir() else None
        sources = resolve_clip_sources(document, paths.out, source_dir)
        plan = build_plan(document, config, story=story, clip_sources=sources, selection=selection)
    except (ReelError, OSError, ValueError, KeyError, TypeError):
        return None
    keys = [segment_key(segment, plan, config) for segment in plan.segments]
    return {
        "cache_dir": str(paths.out / REEL_DIRNAME / SEGMENT_CACHE_DIRNAME),
        "keys": keys,
        "total": len(keys),
    }


def reel_render_progress(progress_json: str) -> dict[str, Any]:
    """Real segment-cache-file counts against the plan computed at job start.

    `done` is a count of files that exist on disk *right now* -- never a fabricated percentage --
    and `total` is `None`, not `0`, until the plan above was actually computed.
    """
    if not progress_json:
        return {
            "stage": "reel:render",
            "done": 0,
            "total": None,
            "detail": (
                "the segment plan has not been computed yet, so nothing is measured. This is not "
                "the same as zero segments to render."
            ),
        }
    try:
        seed = json.loads(progress_json)
    except json.JSONDecodeError:  # pragma: no cover - written by this codebase only
        return {
            "stage": "reel:render",
            "done": 0,
            "total": None,
            "detail": "this job's own progress record could not be read back",
        }
    cache_dir = Path(seed.get("cache_dir", ""))
    keys: list[str] = seed.get("keys") or []
    done = sum(1 for key in keys if (cache_dir / f"{key}.mp4").is_file())
    return {"stage": "reel:render", "done": done, "total": seed.get("total"), "detail": ""}
