"""Pipeline runner: ordering, cache filtering, parallelism, progress, and resume.

The correctness property this module exists to guarantee: **interrupting a run and
re-invoking it recomputes only unfinished work.** Every completed item is committed
immediately (the connection runs in autocommit), so a `SIGINT`, a closed laptop, or an OOM
loses at most one item's work.
"""

from __future__ import annotations

import asyncio
import os
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, StageStatus
from story_book.pipeline.base import (
    TRIP_SENTINEL,
    BatchStage,
    Executor,
    PerItemStage,
    SkipItem,
    Stage,
    StageContext,
    WholeTripStage,
)


@dataclass(slots=True)
class StageReport:
    name: str
    total: int = 0
    cached: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    unavailable_reason: str = ""

    @property
    def was_run(self) -> bool:
        return not self.unavailable_reason


@dataclass(slots=True)
class RunReport:
    stages: list[StageReport] = field(default_factory=list)
    interrupted: bool = False

    @property
    def total_failed(self) -> int:
        return sum(s.failed for s in self.stages)


def worker_count() -> int:
    """Leave headroom so the machine stays usable during a long run."""
    return max(1, (os.cpu_count() or 2) - 2)


def _compute_worker(stage: PerItemStage, media: Media, config: Config) -> Any:
    """Top-level so it is picklable for ProcessPoolExecutor."""
    return stage.compute(media, config)


class Runner:
    def __init__(
        self,
        ctx: StageContext,
        stages: list[Stage],
        *,
        console: Console | None = None,
        dry_run: bool = False,
        force: tuple[str, ...] = (),
    ) -> None:
        self.ctx = ctx
        self.stages = stages
        self.console = console or Console()
        self.dry_run = dry_run
        self.force = set(force)
        self._validate_force()

    def _validate_force(self) -> None:
        known = {stage.name for stage in self.stages}
        unknown = self.force - known - {"all"}
        if unknown:
            raise ValueError(
                f"--force names unknown stage(s): {', '.join(sorted(unknown))}. "
                f"Known stages: {', '.join(sorted(known))}"
            )

    def run(self) -> RunReport:
        report = RunReport()
        for stage in self.stages:
            available, reason = stage.available(self.ctx)
            if not available:
                report.stages.append(StageReport(name=stage.name, unavailable_reason=reason))
                self.console.print(f"[yellow]skip[/] {stage.name}: {reason}")
                continue

            if not self.dry_run and (stage.name in self.force or "all" in self.force):
                cleared = db.clear_stage(self.ctx.conn, stage.name)
                self.console.print(
                    f"[cyan]force[/] {stage.name}: cleared {cleared} cached result(s)"
                )

            try:
                report.stages.append(self._run_stage(stage))
            except KeyboardInterrupt:
                report.interrupted = True
                self.console.print(
                    f"\n[yellow]interrupted during {stage.name}.[/] "
                    "Completed work is saved; re-run to resume."
                )
                break
        self._print_summary(report)
        return report

    def _run_stage(self, stage: Stage) -> StageReport:
        if isinstance(stage, WholeTripStage):
            return self._run_whole_trip(stage)
        if isinstance(stage, BatchStage):
            return self._run_batched(stage)
        if isinstance(stage, PerItemStage):
            return self._run_per_item(stage)
        raise TypeError(f"{stage.name} is not a recognized stage type")

    # --- whole-trip -------------------------------------------------------------------

    def _run_whole_trip(self, stage: WholeTripStage) -> StageReport:
        report = StageReport(name=stage.name, total=1)
        cached = db.completed_hashes(self.ctx.conn, stage.name, stage.version)
        if TRIP_SENTINEL in cached and not stage.always_run:
            report.cached = 1
            return report
        if self.dry_run:
            return report

        try:
            stage.run(self.ctx)
        except Exception as exc:
            db.record_stage_result(
                self.ctx.conn,
                TRIP_SENTINEL,
                stage.name,
                stage.version,
                StageStatus.FAILED,
                _format_error(exc),
            )
            report.failed = 1
            self.console.print(f"[red]fail[/] {stage.name}: {exc}")
            return report

        db.record_stage_result(
            self.ctx.conn, TRIP_SENTINEL, stage.name, stage.version, StageStatus.OK
        )
        report.done = 1
        return report

    # --- per item ---------------------------------------------------------------------

    def _pending(self, stage: Stage, candidates: list[Media]) -> list[Media]:
        if stage.always_run:
            return list(candidates)
        cached = db.completed_hashes(self.ctx.conn, stage.name, stage.version)
        return [m for m in candidates if m.hash not in cached]

    def _run_per_item(self, stage: PerItemStage) -> StageReport:
        candidates = stage.select(self.ctx)
        pending = self._pending(stage, candidates)
        report = StageReport(
            name=stage.name, total=len(candidates), cached=len(candidates) - len(pending)
        )
        if self.dry_run or not pending:
            return report

        if stage.executor is Executor.PROCESS and len(pending) > 1:
            self._drive_process_pool(stage, pending, report)
        elif stage.executor is Executor.ASYNC:
            asyncio.run(self._drive_async(stage, pending, report))
        else:
            self._drive_serial(stage, pending, report)
        return report

    def _drive_serial(self, stage: PerItemStage, pending: list[Media], report: StageReport) -> None:
        with self._progress(stage.name, len(pending)) as (progress, task):
            for media in pending:
                try:
                    payload = stage.compute(media, self.ctx.config)
                except SkipItem as exc:
                    self._record_skip(stage, media, report, str(exc))
                except Exception as exc:
                    self._record_failure(stage, media, report, exc)
                else:
                    self._record_success(stage, media, payload, report)
                progress.advance(task)

    def _drive_process_pool(
        self, stage: PerItemStage, pending: list[Media], report: StageReport
    ) -> None:
        """Bounded submission: keep ~2x workers in flight rather than queueing 8,000 futures."""
        workers = worker_count()
        limit = workers * 2
        queue = iter(pending)
        in_flight: dict[Any, Media] = {}
        executor = ProcessPoolExecutor(max_workers=workers)
        try:
            with self._progress(stage.name, len(pending)) as (progress, task):
                while True:
                    while len(in_flight) < limit:
                        media = next(queue, None)
                        if media is None:
                            break
                        future = executor.submit(_compute_worker, stage, media, self.ctx.config)
                        in_flight[future] = media
                    if not in_flight:
                        break
                    done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                    for future in done:
                        media = in_flight.pop(future)
                        try:
                            payload = future.result()
                        except SkipItem as exc:
                            self._record_skip(stage, media, report, str(exc))
                        except Exception as exc:
                            self._record_failure(stage, media, report, exc)
                        else:
                            self._record_success(stage, media, payload, report)
                        progress.advance(task)
        except BaseException:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    async def _drive_async(
        self, stage: PerItemStage, pending: list[Media], report: StageReport
    ) -> None:
        semaphore = asyncio.Semaphore(worker_count())

        async def guarded(media: Media) -> tuple[Media, Any, Exception | None]:
            async with semaphore:
                try:
                    return media, await stage.compute_async(media, self.ctx.config), None
                except Exception as exc:
                    return media, None, exc

        with self._progress(stage.name, len(pending)) as (progress, task):
            tasks = [asyncio.create_task(guarded(m)) for m in pending]
            try:
                for coro in asyncio.as_completed(tasks):
                    media, payload, error = await coro
                    if isinstance(error, SkipItem):
                        self._record_skip(stage, media, report, str(error))
                    elif error is not None:
                        self._record_failure(stage, media, report, error)
                    else:
                        self._record_success(stage, media, payload, report)
                    progress.advance(task)
            except KeyboardInterrupt:
                for pending_task in tasks:
                    pending_task.cancel()
                raise

    # --- batched ----------------------------------------------------------------------

    def _run_batched(self, stage: BatchStage) -> StageReport:
        candidates = stage.select(self.ctx)
        pending = self._pending(stage, candidates)
        report = StageReport(
            name=stage.name, total=len(candidates), cached=len(candidates) - len(pending)
        )
        if self.dry_run or not pending:
            return report

        with self._progress(stage.name, len(pending)) as (progress, task):
            for start in range(0, len(pending), stage.batch_size):
                batch = pending[start : start + stage.batch_size]
                try:
                    results = stage.process_batch(self.ctx, batch)
                except SkipItem as exc:
                    for media in batch:
                        self._record_skip(stage, media, report, str(exc))
                except Exception as exc:
                    for media in batch:
                        self._record_failure(stage, media, report, exc)
                else:
                    for media in batch:
                        if media.hash in results:
                            db.record_stage_result(
                                self.ctx.conn,
                                media.hash,
                                stage.name,
                                stage.version,
                                StageStatus.OK,
                            )
                            report.done += 1
                        else:
                            db.record_stage_result(
                                self.ctx.conn,
                                media.hash,
                                stage.name,
                                stage.version,
                                StageStatus.FAILED,
                                "not returned by process_batch",
                            )
                            report.failed += 1
                progress.advance(task, len(batch))
        return report

    # --- result recording -------------------------------------------------------------

    def _record_success(
        self, stage: PerItemStage, media: Media, payload: Any, report: StageReport
    ) -> None:
        try:
            stage.persist(self.ctx, media, payload)
        except Exception as exc:
            self._record_failure(stage, media, report, exc)
            return
        db.record_stage_result(self.ctx.conn, media.hash, stage.name, stage.version, StageStatus.OK)
        report.done += 1

    def _record_skip(self, stage: Stage, media: Media, report: StageReport, reason: str) -> None:
        db.record_stage_result(
            self.ctx.conn, media.hash, stage.name, stage.version, StageStatus.SKIPPED, reason
        )
        report.skipped += 1

    def _record_failure(
        self, stage: Stage, media: Media, report: StageReport, exc: Exception
    ) -> None:
        """One bad file must never kill a run over 8,000 of them."""
        db.record_stage_result(
            self.ctx.conn,
            media.hash,
            stage.name,
            stage.version,
            StageStatus.FAILED,
            _format_error(exc),
        )
        report.failed += 1

    # --- presentation -----------------------------------------------------------------

    def _progress(self, label: str, total: int) -> _ProgressHandle:
        return _ProgressHandle(self.console, label, total)

    def _print_summary(self, report: RunReport) -> None:
        table = Table(title="Pipeline summary", header_style="bold")
        for column in ("stage", "total", "cached", "done", "skipped", "failed"):
            table.add_column(column, justify="left" if column == "stage" else "right")
        for stage_report in report.stages:
            if not stage_report.was_run:
                table.add_row(stage_report.name, "-", "-", "-", "-", "-")
                continue
            table.add_row(
                stage_report.name,
                str(stage_report.total),
                str(stage_report.cached),
                str(stage_report.done),
                str(stage_report.skipped),
                f"[red]{stage_report.failed}[/]" if stage_report.failed else "0",
            )
        self.console.print(table)
        if report.total_failed:
            self.console.print(
                f"[yellow]{report.total_failed} item(s) failed.[/] "
                "They are recorded per-stage and will be retried on the next run."
            )


class _ProgressHandle:
    """Rich progress as a context manager yielding (progress, task_id)."""

    def __init__(self, console: Console, label: str, total: int) -> None:
        self._progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self._label = label
        self._total = total
        self._task: Any = None

    def __enter__(self) -> tuple[Progress, Any]:
        self._progress.start()
        self._task = self._progress.add_task(self._label, total=self._total)
        return self._progress, self._task

    def __exit__(self, *exc_info: Any) -> None:
        self._progress.stop()


def _format_error(exc: Exception) -> str:
    """Keep the type and message, plus the last frame -- enough to debug, short enough to store."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {frames[-1].filename}:{frames[-1].lineno}" if frames else ""
    return f"{type(exc).__name__}: {exc}{where}"
