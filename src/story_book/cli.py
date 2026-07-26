"""Command line interface.

Wave 1+ stages register themselves in `build_stages`; nothing else here should need to change
as stages land.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from story_book import __version__, profile_render
from story_book import profile as story_profile
from story_book.config import Config, ConfigError
from story_book.db import connection as db
from story_book.eval import evaluate_truth_set_file, render_report
from story_book.pipeline.base import Stage, StageContext
from story_book.pipeline.embeddings import EmbeddingStage
from story_book.pipeline.landmarks.base import LandmarkStage
from story_book.pipeline.metadata import MetadataStage
from story_book.pipeline.quality import ContentClassStage, QualityStage
from story_book.pipeline.runner import Runner
from story_book.pipeline.scan import ScanStage
from story_book.pipeline.timezones import TimezoneStage
from story_book.pipeline.video import VideoStage
from story_book.profile_json import profile_to_dict

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
             -> [gps_backfill] -> [geocode] -> [days] -> [events]        (Wave 2/3)
             -> video, embeddings, quality, content_class               (independent)
             -> [dedup] -> [selection]                                  (Wave 3)
             -> landmarks
             -> [timeline] -> [report, package]                          (Wave 3/4)

    Bracketed stages are not implemented yet. Every stage declares its own `available()`, so an
    absent binary, a missing optional dependency, or `--no-cloud` skips that stage and the run
    still completes.
    """
    return [
        ScanStage(),
        MetadataStage(),
        TimezoneStage(),
        VideoStage(),
        EmbeddingStage(),
        QualityStage(),
        ContentClassStage(),
        LandmarkStage(),
    ]


def _load_config(config_path: Path | None) -> Config:
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        console.print(f"[red]config error:[/] {exc}")
        raise typer.Exit(2) from exc


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

    trip_name = config.trip_name or source.name
    conn = db.connect(out / db.DB_FILENAME)
    db.ensure_trip(conn, trip_name)

    ctx = StageContext(
        conn=conn,
        config=config,
        out_dir=out,
        source_dir=source,
        no_cloud=config.no_cloud,
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
    if report.total_failed:
        raise typer.Exit(1)


@app.command()
def report(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory to re-render.")],
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Re-render the HTML report from an existing database. Recomputes no pipeline stage."""
    _load_config(config_path)
    db_path = out.resolve() / db.DB_FILENAME
    if not db_path.exists():
        console.print(f"[red]no database at {db_path}[/] -- run `story-book build` first.")
        raise typer.Exit(2)
    db.connect(db_path, create=False)
    console.print(
        "[yellow]Report rendering lands in T40.[/] See dev_plan/implementation_tracker.md."
    )


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
    console.print(render_report(report))


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
