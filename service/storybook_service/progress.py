"""Job progress, read from the pipeline's own `stage_result` table.

**This module fabricates nothing, and that is its entire specification.** I22 (`JobPoller`) says:
*progress is real and can be read from `stage_result` rather than invented -- do not display a
fabricated percentage.* `CLAUDE.md` calls a fabricated number that looks measured this project's
most repeated failure. So:

- Every count here is a row count in `story.db`, taken with the pipeline's own accessors
  (`db.completed_hashes`, `db.stage_failures`, `db.count_media`). No raw SQL touches `media` or
  `stage_result`.
- There is **no percentage, no ETA and no smoothing**. A percentage over eighteen stages would
  assert that an item of `thumbnails` costs what an item of `embeddings` costs, which is not a
  measurement anyone took. What a client gets is the current stage, its two counts, and its
  position in an ordered list -- all facts.
- Where a count cannot be taken, it is `None` with a reason, never zero and never a guess.

Three things a reader of this file should know, because each one surprised me:

**The stage list is read from the pipeline, not copied.** `story_book.cli.build_stages` is the
order and the versions, so a stage added upstream appears here without an edit, and a bumped
`version` invalidates progress exactly as it invalidates the cache.

**`select()` is not a stable denominator on its own.** `EmbeddingStage.select` filters out items it
has already embedded (it has to: the cache key carries no model tag), so as the stage progresses
`len(select())` *shrinks*. Using it raw would produce a total that walked down towards `done` --
a progress bar that fills because the goalpost moved. The denominator here is therefore
`select() ∪ already-completed`, which is monotone for a self-filtering stage and identical to
`select()` for every other one.

**A stage stops being the current one when its items are terminal, not when they all succeeded.**
A permanently failing item leaves `done < total` forever; if "current" meant "first incomplete",
one bad photograph would pin the report to `metadata` for the rest of the run. So a stage is
finished when `done + failed >= total`, and the failures are reported rather than absorbed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from story_book.cli import build_stages
from story_book.config import Config, ConfigError
from story_book.db import connection as db
from story_book.pipeline.base import TRIP_SENTINEL, StageContext, WholeTripStage

OUTSTANDING_STATES = ("pending", "in_progress", "unknown")
"""States that mean "this stage still has work in it".

The current stage is the **first** stage in pipeline order in one of these -- not the first with
rows, because a stage that has recorded nothing yet is still the one being worked on, and not the
last with rows, because that reads a stage behind. `unavailable` and `complete` are skipped over,
which is what keeps the pointer moving forward past a `clip`-less `embeddings` instead of parking
on it for the rest of the run.
"""

PROGRESS_BASIS = (
    "row counts in story.db's stage_result table, taken with the pipeline's own accessors. "
    "No percentage, estimate or elapsed-time extrapolation is published: the stages cost wildly "
    "different amounts per item, so a single percentage would be a fabricated measurement."
)


@dataclass(frozen=True, slots=True)
class StageProgress:
    name: str
    version: int
    state: str
    """`complete`, `in_progress`, `pending`, `unavailable`, or `unknown`."""
    done: int
    failed: int
    total: int | None
    """None when the candidate set could not be counted -- never a substituted zero."""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BuildProgress:
    """What `GET /jobs/{id}` publishes about the build half of a job."""

    stage: str | None
    done: int
    total: int | None
    stage_index: int | None
    """1-based position of `stage` in the pipeline order, so a client can show 5 of 18."""
    stages_total: int
    stages_complete: int
    stages: list[StageProgress] = field(default_factory=list)
    media_known: int = 0
    detail: str = ""
    basis: str = PROGRESS_BASIS


def _unavailable_from_capability(capability: str) -> dict[str, str]:
    """Which stages the worker measured as unavailable, in the stage's own words.

    Measured once, when the job started, and stored on the job row. Re-probing per poll would spawn
    subprocesses for `exiftool` and `ffmpeg` on every request, and -- worse -- would report a
    capability the running build never had.
    """
    if not capability:
        return {}
    try:
        payload = json.loads(capability)
    except json.JSONDecodeError:  # pragma: no cover - written by this codebase only
        return {}
    return {
        entry["stage"]: entry.get("reason", "")
        for entry in payload.get("unavailable", [])
        if entry.get("stage")
    }


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """Open `story.db` strictly read-only, on a separate connection from the worker's.

    `db.connect` would be wrong here twice over: it runs the schema script, and it is the writer's
    entry point. This is the reader, it must not be able to write even by accident, and `mode=ro`
    is the only way to say so. The busy timeout matters because the worker commits per item.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _schema_note(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return ""
    found = int(row["value"])
    if found == db.SCHEMA_VERSION:
        return ""
    return (
        f"story.db is schema version {found}; this service reads {db.SCHEMA_VERSION}. "
        "Stage counts may be incomplete."
    )


def read_build_progress(
    *,
    out_dir: Path,
    source_dir: Path,
    config_path: Path | None,
    capability: str = "",
) -> BuildProgress:
    """Read the build's progress. Never raises for a missing or busy database."""
    db_path = out_dir / db.DB_FILENAME
    if not db_path.exists():
        return BuildProgress(
            stage=None,
            done=0,
            total=None,
            stage_index=None,
            stages_total=0,
            stages_complete=0,
            detail=(
                f"no {db.DB_FILENAME} under {out_dir}: the build has not opened its database yet, "
                "so there is nothing measured to report."
            ),
        )

    try:
        config = Config.load(config_path) if config_path and config_path.exists() else Config()
    except ConfigError as exc:
        config = Config()
        config_note = f"config could not be loaded ({exc}); stage defaults assumed"
    else:
        config_note = ""

    conn = _read_only_connection(db_path)
    try:
        note = "; ".join(part for part in (_schema_note(conn), config_note) if part)
        ctx = StageContext(
            conn=conn,
            config=config,
            out_dir=out_dir,
            source_dir=source_dir,
            no_cloud=config.no_cloud,
        )
        unavailable = _unavailable_from_capability(capability)
        stages = build_stages(ctx)
        media_known = db.count_media(conn)

        reports: list[StageProgress] = []
        current_index: int | None = None
        for position, stage in enumerate(stages, start=1):
            report = _stage_progress(ctx, stage, unavailable, media_known=media_known)
            reports.append(report)
            if current_index is None and report.state in OUTSTANDING_STATES:
                current_index = position

        complete = sum(1 for report in reports if report.state == "complete")
        if current_index is None:
            return BuildProgress(
                stage=None,
                done=0,
                total=None,
                stage_index=None,
                stages_total=len(reports),
                stages_complete=complete,
                stages=reports,
                media_known=media_known,
                detail="; ".join(
                    part
                    for part in (
                        note,
                        "every stage is complete or unavailable, so stage_result shows no "
                        "outstanding work. A rebuild of an unchanged library starts in this state, "
                        "because the work is already cached -- that is resumability, not an error.",
                    )
                    if part
                ),
            )

        current = reports[current_index - 1]
        return BuildProgress(
            stage=current.name,
            done=current.done,
            total=current.total,
            stage_index=current_index,
            stages_total=len(reports),
            stages_complete=complete,
            stages=reports,
            media_known=media_known,
            detail=note,
        )
    finally:
        conn.close()


def _stage_progress(
    ctx: StageContext, stage: Any, unavailable: dict[str, str], *, media_known: int
) -> StageProgress:
    if stage.name in unavailable:
        return StageProgress(
            name=stage.name,
            version=stage.version,
            state="unavailable",
            done=0,
            failed=0,
            total=None,
            # The stage's own reason, quoted rather than paraphrased. `EmbeddingStage` explains
            # what a missing `clip` costs better than this module could.
            detail=unavailable[stage.name],
        )

    try:
        completed = db.completed_hashes(ctx.conn, stage.name, stage.version)
        failed = {result.media_hash for result in db.stage_failures(ctx.conn, stage.name)}
    except sqlite3.Error as exc:  # pragma: no cover - a busy or damaged database
        return StageProgress(
            name=stage.name,
            version=stage.version,
            state="unknown",
            done=0,
            failed=0,
            total=None,
            detail=f"stage_result could not be read: {exc}",
        )

    if isinstance(stage, WholeTripStage):
        # One cache row under the trip sentinel, so the honest denominator is 1. An `always_run`
        # aggregate keeps its row from the previous build, which is why a second build of an
        # unchanged trip legitimately reads as already complete.
        done = 1 if TRIP_SENTINEL in completed else 0
        return StageProgress(
            name=stage.name,
            version=stage.version,
            state="complete" if done or TRIP_SENTINEL in failed else "pending",
            done=done,
            failed=1 if TRIP_SENTINEL in failed else 0,
            total=1,
            detail="whole-trip stage: one aggregate result, not per-item counts",
        )

    try:
        candidates = {media.hash for media in stage.select(ctx)}
    except Exception as exc:
        # Read-only by construction, so a stage whose `select` needs to write cannot be counted
        # here. Reported as uncountable rather than as zero -- a total of 0 would read as a stage
        # with nothing to do.
        return StageProgress(
            name=stage.name,
            version=stage.version,
            state="unknown",
            done=len(completed),
            failed=len(failed),
            total=None,
            detail=f"candidate count unavailable from a read-only connection: {exc}",
        )

    # The union, for the reason in the module docstring: a stage that filters its own work list
    # against a table it wrote reports a shrinking `select()`, and a denominator that walks towards
    # the numerator is a progress bar that fills without progress.
    total = len(candidates | completed | failed)
    done = len(completed)
    failures = len(failed - completed)
    if total == 0 and media_known == 0:
        # **Found by reading the real response, not by a test.** Before `scan` has committed, the
        # media table is empty, so every per-item stage selects nothing -- and reporting that as
        # "complete, does not apply" made a job one second old claim five of eighteen stages done.
        # An empty library is not evidence that a stage has nothing to do.
        state, detail, total = (
            "pending",
            "the library has not been scanned yet, so this stage's candidate set is not yet known",
            None,
        )
    elif total == 0:
        state, detail = "complete", "no candidate items: this stage does not apply to this library"
    elif done + failures >= total:
        state, detail = "complete", ""
    elif done or failures:
        state, detail = "in_progress", ""
    else:
        state, detail = "pending", ""
    return StageProgress(
        name=stage.name,
        version=stage.version,
        state=state,
        done=done,
        failed=failures,
        total=total,
        detail=detail,
    )
