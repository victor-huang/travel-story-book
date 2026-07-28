"""Command line interface.

Wave 1+ stages register themselves in `build_stages`; nothing else here should need to change
as stages land.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from story_book import __version__, profile_render
from story_book import profile as story_profile
from story_book.config import Config, ConfigError
from story_book.db import connection as db
from story_book.eval import evaluate_truth_set_file
from story_book.eval import render_report as render_eval_report
from story_book.export.package import ORIGINALS, PREVIEW, build_package, write_archive
from story_book.export.report import render_report
from story_book.overrides import OverrideError, Overrides
from story_book.pipeline.base import Stage, StageContext
from story_book.pipeline.days import DaysStage
from story_book.pipeline.dedup import DedupStage, PhashStage
from story_book.pipeline.embeddings import EmbeddingStage
from story_book.pipeline.events import EventStage
from story_book.pipeline.geocode import GeocodeStage
from story_book.pipeline.gps_backfill import GpsBackfillStage
from story_book.pipeline.home_filter import HomeFilterStage
from story_book.pipeline.landmarks.base import LandmarkStage
from story_book.pipeline.metadata import MetadataStage
from story_book.pipeline.quality import ContentClassStage, QualityStage
from story_book.pipeline.runner import Runner
from story_book.pipeline.scan import ScanStage
from story_book.pipeline.selection import SelectionStage
from story_book.pipeline.thumbnails import ThumbnailStage
from story_book.pipeline.timeline import TRIP_JSON_FILENAME, TimelineStage, build_timeline
from story_book.pipeline.timezones import TimezoneStage
from story_book.pipeline.video import VideoStage
from story_book.profile_json import profile_to_dict
from story_book.trip_context import TripContext, TripContextError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Turn one trip's photos and videos into an organized, browsable story.",
)
console = Console()


def build_stages(ctx: StageContext) -> list[Stage]:
    """The pipeline, in dependency order.

    The corrected order from the plan doc, with unbuilt stages marked. Landmark recognition sits
    deliberately *after* selection so it only ever sees a few hundred representatives rather than
    every photo, and event detection must never consume landmark labels -- that circularity was
    the original draft's bug.

        scan -> metadata -> timezones
             -> gps_backfill -> geocode -> days -> events -> home_filter
             -> video, embeddings, quality, content_class                (independent)
             -> phash -> dedup -> selection
             -> landmarks
             -> thumbnails -> timeline -> [report, package]              (Wave 4)

    Bracketed stages are not implemented yet. Every stage declares its own `available()`, so an
    absent binary, a missing optional dependency, or `--no-cloud` skips that stage and the run
    still completes.
    """
    return [
        ScanStage(),
        MetadataStage(),
        TimezoneStage(),
        # Location before anything that reads it: geocoding and the home filter must see
        # interpolated coordinates, not just measured ones -- otherwise a GPS-less photo taken at
        # home would skip the privacy check entirely.
        GpsBackfillStage(),
        GeocodeStage(),
        DaysStage(),
        EventStage(),
        HomeFilterStage(),
        VideoStage(),
        EmbeddingStage(),
        QualityStage(),
        ContentClassStage(),
        PhashStage(),
        DedupStage(),
        SelectionStage(),
        LandmarkStage(),
        ThumbnailStage(),
        TimelineStage(),
    ]


def _load_config(config_path: Path | None) -> Config:
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        console.print(f"[red]config error:[/] {exc}")
        raise typer.Exit(2) from exc


def _overrides_path(explicit: Path | None, config_path: Path | None) -> Path | None:
    """Where to look for corrections: the flag, else `overrides.toml` beside the config.

    An explicit `--overrides` that does not exist is an error rather than a silent no-op -- the
    user asked for a specific file, and quietly ignoring it looks identical to an override that
    failed to take effect. The implicit one is optional by design.

    The implicit lookup is anchored to the config, never to the current directory. Falling back
    to the cwd makes the same command mean different things depending on where it is run, which
    is how a stray `overrides.toml` in a checkout ends up silently applied to an unrelated trip.
    With no `--config`, corrections must be named explicitly.
    """
    if explicit is not None:
        if not explicit.exists():
            console.print(f"[red]overrides error:[/] file not found: {explicit}")
            raise typer.Exit(2)
        return explicit
    if config_path is None:
        return None
    beside = config_path.parent / "overrides.toml"
    return beside if beside.exists() else None


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"story-book {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
) -> None:
    pass


@app.command()
def build(
    source: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="One trip's media folder.")
    ],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.toml.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would run, compute nothing.")
    ] = False,
    no_cloud: Annotated[bool, typer.Option("--no-cloud", help="Skip every network call.")] = False,
    force: Annotated[
        list[str] | None,
        typer.Option("--force", help="Recompute a stage by name, or 'all'. Repeatable."),
    ] = None,
    transcribe: Annotated[
        str | None, typer.Option("--transcribe", help="none | auto | all.")
    ] = None,
    include_all: Annotated[
        bool, typer.Option("--include-all", help="Export unselected media too.")
    ] = False,
    context_path: Annotated[
        Path | None,
        typer.Option("--context", help="Trip context TOML: travellers, voice, plans, notes."),
    ] = None,
    overrides_path: Annotated[
        Path | None,
        typer.Option(
            "--overrides", help="Corrections TOML. Defaults to overrides.toml if present."
        ),
    ] = None,
) -> None:
    """Run the pipeline. Safe to interrupt and re-run: finished work is not recomputed."""
    config = _load_config(config_path)
    if no_cloud:
        config = _with(config, no_cloud=True)
    if transcribe is not None:
        _validate_transcribe(transcribe)

    source = source.resolve()
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    # The one input that cannot be extracted from the media. Absent is fine and common -- the
    # journal is simply more impersonal, and the package says so rather than inventing feelings.
    try:
        trip_context = TripContext.load(context_path)
    except TripContextError as exc:
        console.print(f"[red]trip context error:[/] {exc}")
        raise typer.Exit(2) from exc
    if trip_context.is_empty:
        console.print(
            "[dim]no trip context supplied; the journal will stay factual. "
            "See trip_context.example.toml.[/]"
        )
    else:
        console.print(f"trip context: {len(trip_context.travelers)} traveller(s) described")

    try:
        overrides = Overrides.load(_overrides_path(overrides_path, config_path))
    except OverrideError as exc:
        console.print(f"[red]overrides error:[/] {exc}")
        raise typer.Exit(2) from exc
    if not overrides.is_empty:
        console.print(
            f"overrides: {len(overrides.pin)} pinned, {len(overrides.reject)} rejected, "
            f"{len(overrides.keeper)} forced keeper(s)"
        )

    trip_name = config.trip_name or source.name
    conn = db.connect(out / db.DB_FILENAME)
    db.ensure_trip(conn, trip_name)

    ctx = StageContext(
        conn=conn,
        config=config,
        out_dir=out,
        source_dir=source,
        no_cloud=config.no_cloud,
        overrides=overrides,
        trip_context=trip_context,
    )
    stages = build_stages(ctx)
    console.print(f"trip: [bold]{trip_name}[/]  ({db.count_media(conn)} media known)")
    if include_all:
        console.print("[cyan]--include-all[/]: unselected media will also be exported")

    try:
        runner = Runner(ctx, stages, console=console, dry_run=dry_run, force=tuple(force or ()))
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    report = runner.run()
    if report.interrupted:
        raise typer.Exit(130)

    # The report is the main deliverable and costs a fraction of a second, so `build` always
    # leaves one behind. The package is not automatic: it duplicates media, and that should be
    # an explicit request.
    trip_json = out / TRIP_JSON_FILENAME
    if trip_json.exists():
        rendered = render_report(json.loads(trip_json.read_text()), out)
        console.print(f"report: [bold]{rendered.index}[/]")

    if report.total_failed:
        raise typer.Exit(1)


@app.command()
def report(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory to re-render.")],
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    context_path: Annotated[Path | None, typer.Option("--context")] = None,
) -> None:
    """Re-render the HTML report from an existing database. Recomputes no pipeline stage.

    Rebuilds `trip.json` from the DB and renders from that, rather than reading a `trip.json`
    that may predate the last `build`. Derived images are reused as they are -- this command is
    for iterating on the report, and re-encoding thumbnails is exactly the expensive thing it
    exists to avoid.
    """
    config = _load_config(config_path)
    out = out.resolve()
    db_path = out / db.DB_FILENAME
    if not db_path.exists():
        console.print(f"[red]no database at {db_path}[/] -- run `story-book build` first.")
        raise typer.Exit(2)

    try:
        trip_context = TripContext.load(context_path)
    except TripContextError as exc:
        console.print(f"[red]trip context error:[/] {exc}")
        raise typer.Exit(2) from exc

    started = time.monotonic()
    conn = db.connect(db_path, create=False)
    document = build_timeline(conn, config, trip_context, out)
    (out / TRIP_JSON_FILENAME).write_text(json.dumps(document, indent=2) + "\n")
    rendered = render_report(document, out)
    elapsed = time.monotonic() - started

    missing = sum(1 for a in document["assets"].values() if not a["thumbnail"])
    if missing:
        console.print(
            f"[yellow]{missing} item(s) have no thumbnail[/] -- run `story-book build` to "
            "generate them."
        )
    console.print(f"{rendered.page_count} page(s) in {elapsed:.1f}s -> [bold]{rendered.index}[/]")


@app.command()
def package(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory holding story.db.")],
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    context_path: Annotated[Path | None, typer.Option("--context")] = None,
    originals: Annotated[
        bool,
        typer.Option(
            "--originals",
            help="Ship full-resolution originals instead of previews. Hardlinks where possible.",
        ),
    ] = False,
    archive: Annotated[
        bool,
        typer.Option("--zip", help="Also write package.zip, without macOS filesystem droppings."),
    ] = False,
) -> None:
    """Build the ChatGPT upload package: contact sheets, brief, prompt, and a manifest.

    Previews by default. Originals are only worth the disk when someone needs to judge focus or
    crop headroom, and the manifest states which kind the package holds either way.
    """
    config = _load_config(config_path)
    out = out.resolve()
    db_path = out / db.DB_FILENAME
    if not db_path.exists():
        console.print(f"[red]no database at {db_path}[/] -- run `story-book build` first.")
        raise typer.Exit(2)

    try:
        trip_context = TripContext.load(context_path)
    except TripContextError as exc:
        console.print(f"[red]trip context error:[/] {exc}")
        raise typer.Exit(2) from exc

    conn = db.connect(db_path, create=False)
    document = build_timeline(conn, config, trip_context, out)
    # trip.json deliberately carries no absolute paths -- it is a thing you hand to someone
    # else -- so the mapping to originals is assembled here, where the DB is in reach.
    sources = {
        asset["asset_id"]: Path(row["path"])
        for asset in document["assets"].values()
        for row in conn.execute("SELECT path FROM media WHERE hash = ?", (asset["content_hash"],))
    }
    built = build_package(
        document, out, mode=ORIGINALS if originals else PREVIEW, source_for=sources
    )

    for skipped_name, reason in built.skipped:
        console.print(f"[yellow]skipped[/] {skipped_name}: {reason}")
    console.print(
        f"{len(built.days)} day(s), {sum(len(d.sheets) for d in built.days)} contact sheet(s) "
        f"[{built.mode}] -> [bold]{built.root}[/]"
    )
    if archive:
        target = write_archive(built)
        size_mb = target.stat().st_size / 1_048_576
        console.print(f"archive: [bold]{target}[/] ({size_mb:.0f} MB, no .DS_Store)")
    console.print("Open a fresh chat per day; attach the sheets and brief.md, paste prompt.md.")


@app.command(name="eval")
def eval_command(
    truth_set: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Hand-labelled truth set TOML.")
    ],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory holding story.db.")],
) -> None:
    """Score the pipeline against a hand-labelled truth set.

    Reports event-boundary precision/recall, duplicate-cluster pairwise precision/recall, and
    keeper agreement, with the Phase 1 targets from the plan. See `docs/truth_set.md` for the
    format and labelling guidance.
    """
    db_path = out.resolve() / db.DB_FILENAME
    if not db_path.exists():
        console.print(f"[red]no database at {db_path}[/] -- run `story-book build` first.")
        raise typer.Exit(2)

    conn = db.connect(db_path, create=False)
    report = evaluate_truth_set_file(conn, truth_set)
    console.print(render_eval_report(report))


@app.command()
def profile(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the raw profile as JSON.")
    ] = None,
) -> None:
    """Summarize a media folder: counts, devices, date range, GPS and timezone coverage.

    Reads nothing but metadata and writes no database. Run this before `build` -- its suggested
    config replaces the guessed defaults that every later stage depends on.
    """
    source = source.resolve()
    with console.status(f"scanning {source}..."):
        result = story_profile.run(source)
    profile_render.render(result, console)

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(profile_to_dict(result), indent=2))
        console.print(f"wrote {json_out}")


def _validate_transcribe(value: str) -> None:
    if value not in {"none", "auto", "all"}:
        console.print(f"[red]--transcribe must be none, auto, or all; got {value!r}[/]")
        raise typer.Exit(2)


def _with(config: Config, **changes: object) -> Config:
    return replace(config, **changes)  # type: ignore[arg-type]


if __name__ == "__main__":
    app()
