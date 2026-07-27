"""Runner behavior, above all the resumability guarantee.

Stages are defined at module level so they are picklable for the process-pool tests.
Computed hashes are recorded in a file rather than memory so worker processes can report back.
"""

from __future__ import annotations

import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from story_book.config import Config
from story_book.db import connection as db
from story_book.db.models import Media, StageStatus
from story_book.pipeline.base import (
    TRIP_SENTINEL,
    BatchStage,
    Executor,
    PerItemStage,
    SkipItem,
    StageContext,
    WholeTripStage,
)
from story_book.pipeline.runner import Runner

LEDGER_ENV = "STORY_BOOK_TEST_LEDGER"


def _record(media_hash: str) -> None:
    path = os.environ.get(LEDGER_ENV)
    if path:
        with open(path, "a") as handle:
            handle.write(f"{media_hash}\n")


def ledger_entries() -> list[str]:
    path = Path(os.environ[LEDGER_ENV])
    if not path.exists():
        return []
    return path.read_text().split()


class CountingStage(PerItemStage):
    """Records every item it computes, so tests can assert what was recomputed."""

    name = "counting"
    version = 1

    def select(self, ctx: StageContext) -> list[Media]:
        return list(db.iter_media(ctx.conn))

    def compute(self, media: Media, config: Config) -> Any:
        _record(media.hash)
        return media.hash.upper()

    def persist(self, ctx: StageContext, media: Media, payload: Any) -> None:
        ctx.conn.execute(
            "INSERT OR REPLACE INTO score (media_hash, content_class) VALUES (?, ?)",
            (media.hash, payload),
        )


class ProcessStage(CountingStage):
    name = "process_stage"
    executor = Executor.PROCESS


class AsyncStage(CountingStage):
    name = "async_stage"
    executor = Executor.ASYNC


class InterruptingStage(CountingStage):
    """Raises a genuine SIGINT partway through, simulating a real ctrl-C."""

    name = "counting"
    version = 1
    interrupt_after = 40

    def compute(self, media: Media, config: Config) -> Any:
        if len(ledger_entries()) >= self.interrupt_after:
            os.kill(os.getpid(), signal.SIGINT)
        _record(media.hash)
        return media.hash.upper()


class FailingStage(CountingStage):
    """Shares CountingStage's name so a retry-after-failure test can pair the two."""

    name = "counting"

    def compute(self, media: Media, config: Config) -> Any:
        if media.hash == "item005":
            raise ValueError("bad file")
        return super().compute(media, config)


class SkippingStage(CountingStage):
    name = "skipping"

    def compute(self, media: Media, config: Config) -> Any:
        raise SkipItem("does not apply")


class PersistFailingStage(CountingStage):
    name = "persist_failing"

    def persist(self, ctx: StageContext, media: Media, payload: Any) -> None:
        raise RuntimeError("write failed")


class UnavailableStage(CountingStage):
    name = "unavailable"

    def available(self, ctx: StageContext) -> tuple[bool, str]:
        return False, "exiftool not installed"


class TripStage(WholeTripStage):
    name = "trip_stage"
    version = 1

    def run(self, ctx: StageContext) -> None:
        _record(TRIP_SENTINEL)


class FailingTripStage(WholeTripStage):
    name = "failing_trip"
    version = 1

    def run(self, ctx: StageContext) -> None:
        raise ValueError("aggregate failed")


class DroppingBatchStage(BatchStage):
    """Returns results for all but one item, which must be recorded as failed."""

    name = "dropping_batch"
    version = 1
    batch_size = 4

    def select(self, ctx: StageContext) -> list[Media]:
        return list(db.iter_media(ctx.conn))

    def process_batch(self, ctx: StageContext, batch: list[Media]) -> dict[str, Any]:
        return {m.hash: m.hash.upper() for m in batch if m.hash != "item003"}


@pytest.fixture(autouse=True)
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ledger.txt"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    return path


@pytest.fixture
def populated(conn: sqlite3.Connection, make_media) -> sqlite3.Connection:
    for index in range(100):
        db.upsert_media(
            conn,
            make_media(
                f"item{index:03d}",
                taken_utc=f"2026-07-18T{index // 60:02d}:{index % 60:02d}:00+00:00",
            ),
        )
    return conn


@pytest.fixture
def trip_ctx(populated: sqlite3.Connection, config: Config, out_dir: Path, source_dir: Path):
    return StageContext(conn=populated, config=config, out_dir=out_dir, source_dir=source_dir)


class TestPerItemStage:
    def test_all_items_are_processed(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        assert len(ledger_entries()) == 100

    def test_payload_is_persisted(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        row = trip_ctx.conn.execute(
            "SELECT content_class FROM score WHERE media_hash = 'item000'"
        ).fetchone()
        assert row["content_class"] == "ITEM000"

    def test_report_counts_completed_items(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [CountingStage()]).run()
        assert report.stages[0].done == 100

    def test_rerun_recomputes_nothing(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage()]).run()
        assert ledger_entries() == []

    def test_rerun_reports_everything_as_cached(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        report = Runner(trip_ctx, [CountingStage()]).run()
        assert report.stages[0].cached == 100


class TestResumeAfterInterrupt:
    """The guarantee the whole caching design exists to provide."""

    def test_interrupt_is_reported(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [InterruptingStage()]).run()
        assert report.interrupted is True

    def test_work_completed_before_the_interrupt_is_saved(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [InterruptingStage()]).run()
        assert len(db.completed_hashes(trip_ctx.conn, "counting", 1)) == 40

    def test_resume_recomputes_only_unfinished_items(
        self, trip_ctx: StageContext, ledger: Path
    ) -> None:
        Runner(trip_ctx, [InterruptingStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage()]).run()
        assert len(ledger_entries()) == 60

    def test_resume_does_not_reprocess_the_first_item(
        self, trip_ctx: StageContext, ledger: Path
    ) -> None:
        Runner(trip_ctx, [InterruptingStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage()]).run()
        assert "item000" not in ledger_entries()

    def test_resume_finishes_every_item(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [InterruptingStage()]).run()
        Runner(trip_ctx, [CountingStage()]).run()
        assert len(db.completed_hashes(trip_ctx.conn, "counting", 1)) == 100

    def test_later_stages_are_not_started_after_an_interrupt(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [InterruptingStage(), TripStage()]).run()
        assert db.completed_hashes(trip_ctx.conn, "trip_stage", 1) == set()


class TestVersionInvalidation:
    def test_bumping_the_version_recomputes_the_stage(
        self, trip_ctx: StageContext, ledger: Path
    ) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        ledger.unlink()

        bumped = CountingStage()
        bumped.version = 2
        Runner(trip_ctx, [bumped]).run()
        assert len(ledger_entries()) == 100

    def test_bumping_one_stage_does_not_invalidate_another(
        self, trip_ctx: StageContext, ledger: Path
    ) -> None:
        Runner(trip_ctx, [CountingStage(), TripStage()]).run()
        ledger.unlink()

        bumped = CountingStage()
        bumped.version = 2
        Runner(trip_ctx, [bumped, TripStage()]).run()
        assert TRIP_SENTINEL not in ledger_entries()


class TestForce:
    def test_force_recomputes_the_named_stage(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [CountingStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage()], force=("counting",)).run()
        assert len(ledger_entries()) == 100

    def test_force_all_recomputes_every_stage(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [CountingStage(), TripStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage(), TripStage()], force=("all",)).run()
        assert TRIP_SENTINEL in ledger_entries()

    def test_force_leaves_other_stages_cached(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [CountingStage(), TripStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage(), TripStage()], force=("counting",)).run()
        assert TRIP_SENTINEL not in ledger_entries()

    def test_unknown_stage_name_is_rejected_early(self, trip_ctx: StageContext) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            Runner(trip_ctx, [CountingStage()], force=("nope",))


class TestDryRun:
    def test_dry_run_computes_nothing(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [CountingStage()], dry_run=True).run()
        assert ledger_entries() == []

    def test_dry_run_still_reports_the_pending_count(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [CountingStage()], dry_run=True).run()
        assert report.stages[0].total == 100

    def test_dry_run_does_not_populate_the_cache(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [CountingStage()], dry_run=True).run()
        assert db.completed_hashes(trip_ctx.conn, "counting", 1) == set()


class TestFailureIsolation:
    def test_one_bad_item_does_not_stop_the_stage(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [FailingStage()]).run()
        assert report.stages[0].done == 99

    def test_the_failure_is_counted(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [FailingStage()]).run()
        assert report.stages[0].failed == 1

    def test_the_failure_records_a_useful_error(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [FailingStage()]).run()
        failure = db.stage_failures(trip_ctx.conn, "counting")[0]
        assert "ValueError: bad file" in failure.error

    def test_a_failed_item_is_retried_on_the_next_run(
        self, trip_ctx: StageContext, ledger: Path
    ) -> None:
        Runner(trip_ctx, [FailingStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [CountingStage()]).run()
        assert ledger_entries() == ["item005"]

    def test_a_persist_error_is_recorded_as_a_failure(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [PersistFailingStage()]).run()
        assert report.stages[0].failed == 100

    def test_a_later_stage_still_runs_after_failures(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [FailingStage(), TripStage()]).run()
        assert TRIP_SENTINEL in ledger_entries()


class TestSkip:
    def test_skipped_items_are_counted_separately(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [SkippingStage()]).run()
        assert report.stages[0].skipped == 100

    def test_skipped_items_are_not_retried(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [SkippingStage()]).run()
        report = Runner(trip_ctx, [SkippingStage()]).run()
        assert report.stages[0].cached == 100

    def test_the_skip_reason_is_recorded(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [SkippingStage()]).run()
        result = db.get_stage_result(trip_ctx.conn, "item000", "skipping")
        assert result.status is StageStatus.SKIPPED and result.error == "does not apply"


class TestUnavailableStage:
    def test_an_unavailable_stage_does_not_run(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [UnavailableStage()]).run()
        assert ledger_entries() == []

    def test_the_reason_is_reported(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [UnavailableStage()]).run()
        assert report.stages[0].unavailable_reason == "exiftool not installed"

    def test_the_pipeline_continues_past_it(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [UnavailableStage(), TripStage()]).run()
        assert TRIP_SENTINEL in ledger_entries()


class TestWholeTripStage:
    def test_it_runs_once(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [TripStage()]).run()
        assert ledger_entries() == [TRIP_SENTINEL]

    def test_it_is_cached_under_the_sentinel(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [TripStage()]).run()
        assert db.completed_hashes(trip_ctx.conn, "trip_stage", 1) == {TRIP_SENTINEL}

    def test_it_does_not_rerun(self, trip_ctx: StageContext, ledger: Path) -> None:
        Runner(trip_ctx, [TripStage()]).run()
        ledger.unlink()
        Runner(trip_ctx, [TripStage()]).run()
        assert ledger_entries() == []

    def test_a_failure_is_recorded_rather_than_raised(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [FailingTripStage()]).run()
        assert report.stages[0].failed == 1

    def test_a_failed_trip_stage_retries_next_run(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [FailingTripStage()]).run()
        assert db.completed_hashes(trip_ctx.conn, "failing_trip", 1) == set()


class TestBatchStage:
    def test_returned_items_succeed(self, trip_ctx: StageContext) -> None:
        report = Runner(trip_ctx, [DroppingBatchStage()]).run()
        assert report.stages[0].done == 99

    def test_a_silently_dropped_item_is_recorded_as_failed(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [DroppingBatchStage()]).run()
        failures = db.stage_failures(trip_ctx.conn, "dropping_batch")
        assert [f.media_hash for f in failures] == ["item003"]

    def test_batches_resume_per_item(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [DroppingBatchStage()]).run()
        report = Runner(trip_ctx, [DroppingBatchStage()]).run()
        assert report.stages[0].cached == 99


class TestProcessPool:
    def test_every_item_is_processed_across_workers(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [ProcessStage()]).run()
        assert len(db.completed_hashes(trip_ctx.conn, "process_stage", 1)) == 100

    def test_results_are_persisted_in_the_parent(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [ProcessStage()]).run()
        count = trip_ctx.conn.execute("SELECT COUNT(*) AS n FROM score").fetchone()["n"]
        assert count == 100

    def test_a_rerun_recomputes_nothing(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [ProcessStage()]).run()
        report = Runner(trip_ctx, [ProcessStage()]).run()
        assert report.stages[0].cached == 100


class TestAsyncExecutor:
    def test_every_item_is_processed(self, trip_ctx: StageContext) -> None:
        Runner(trip_ctx, [AsyncStage()]).run()
        assert len(db.completed_hashes(trip_ctx.conn, "async_stage", 1)) == 100


class TestForceClearsDerivedRows:
    """`--force` on a stage that filters against its own table must empty that table too.

    Found in T43: `--force embeddings` cleared the cache, `select()` saw 277 embeddings already
    present and selected nothing, and the DB was left holding 277 embeddings with zero cache
    rows. The force was a silent no-op and the bookkeeping was permanently inconsistent.
    """

    def test_a_stage_with_no_derived_rows_reports_none(self, ctx: StageContext) -> None:
        from story_book.pipeline.scan import ScanStage

        assert ScanStage().clear_derived(ctx) == 0

    def test_the_embedding_stage_empties_its_own_table(self, ctx: StageContext, make_media) -> None:
        from story_book.db import connection as db_conn
        from story_book.pipeline.embeddings import EmbeddingStage

        db_conn.upsert_media(ctx.conn, make_media("h"))
        ctx.conn.execute(
            "INSERT INTO embedding (media_hash, model, dim, vector) VALUES ('h', 'm', 2, X'00')"
        )
        ctx.conn.commit()

        assert EmbeddingStage().clear_derived(ctx) == 1
        assert ctx.conn.execute("SELECT COUNT(*) AS n FROM embedding").fetchone()["n"] == 0

    def test_the_runner_calls_it_when_forcing(self, ctx: StageContext, mocker) -> None:
        from story_book.pipeline.embeddings import EmbeddingStage

        stage = EmbeddingStage()
        spy = mocker.patch.object(stage, "clear_derived", return_value=0)
        Runner(ctx, [stage], force=("embeddings",)).run()

        spy.assert_called_once()

    def test_the_runner_does_not_call_it_without_force(self, ctx: StageContext, mocker) -> None:
        from story_book.pipeline.embeddings import EmbeddingStage

        stage = EmbeddingStage()
        spy = mocker.patch.object(stage, "clear_derived", return_value=0)
        Runner(ctx, [stage]).run()

        spy.assert_not_called()
