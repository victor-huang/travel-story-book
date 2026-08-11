"""The queue itself: claiming, ownership, and the invariants the schema enforces.

Two of these are the whole reason the queue lives in the index rather than in a caller: **one
active job per trip** and **two workers never take one job**. Both are asserted here, and both are
asserted with a control that must fail if the enforcement is removed.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from storybook_service.index import IndexError_
from storybook_service.index_sqlite import SqliteIndex, new_id


@pytest.fixture
def index(tmp_path):
    """A file, not `:memory:` -- WAL and cross-connection reads are the point of this store."""
    store = SqliteIndex(tmp_path / "index.db")
    yield store
    store.close()


@pytest.fixture
def trip(index):
    user = index.ensure_user(kind="email", value="traveller@example.com")
    return index.create_trip(owner_id=user.id, name="Salzburg", trip_id=new_id())


def _queue(index, trip) -> str:
    job, created = index.enqueue_job(
        owner_id=trip.owner_id, trip_id=trip.id, kind="build", job_id=new_id()
    )
    assert created
    return job.id


class TestWalIsOn:
    def test_the_index_file_is_in_wal_mode(self, tmp_path):
        """Question 19 makes this a requirement: the worker writes while the API reads."""
        store = SqliteIndex(tmp_path / "index.db")
        try:
            probe = sqlite3.connect(tmp_path / "index.db")
            mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
            probe.close()
        finally:
            store.close()
        assert mode.lower() == "wal"


class TestEnqueue:
    def test_a_build_is_queued(self, index, trip):
        job, created = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=trip.id, kind="build", job_id=new_id()
        )
        assert (created, job.state, job.attempts) == (True, "queued", 0)

    def test_a_second_request_returns_the_first_job(self, index, trip):
        """One active job per trip. The client that lost its job_id gets it back."""
        first = _queue(index, trip)
        job, created = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=trip.id, kind="build", job_id=new_id()
        )
        assert (job.id, created) == (first, False)

    def test_another_trip_gets_its_own_job(self, index, trip):
        """The control: the constraint is per trip, not global."""
        other = index.create_trip(owner_id=trip.owner_id, name="Vienna", trip_id=new_id())
        _queue(index, trip)
        _, created = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=other.id, kind="build", job_id=new_id()
        )
        assert created

    def test_a_finished_job_stops_blocking_the_next_one(self, index, trip):
        job_id = _queue(index, trip)
        index.finish_job(job_id=job_id, state="succeeded", at=datetime.now(UTC), exit_code=0)
        _, created = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=trip.id, kind="build", job_id=new_id()
        )
        assert created

    def test_a_running_job_still_blocks_a_second(self, index, trip):
        _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        _, created = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=trip.id, kind="build", job_id=new_id()
        )
        assert not created

    def test_an_unknown_kind_is_refused(self, index, trip):
        with pytest.raises(IndexError_, match="job kind"):
            index.enqueue_job(owner_id=trip.owner_id, trip_id=trip.id, kind="reel", job_id=new_id())


class TestClaim:
    def test_claiming_marks_the_job_running_and_counts_the_attempt(self, index, trip):
        job_id = _queue(index, trip)
        claimed = index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        assert claimed is not None
        assert (claimed.id, claimed.state, claimed.attempts, claimed.worker_id) == (
            job_id,
            "running",
            1,
            "w1",
        )

    def test_an_empty_queue_claims_nothing(self, index):
        assert index.claim_next_job(worker_id="w1", now=datetime.now(UTC)) is None

    def test_a_claimed_job_is_not_claimed_again(self, index, trip):
        _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        assert index.claim_next_job(worker_id="w2", now=datetime.now(UTC)) is None

    def test_the_oldest_queued_job_is_claimed_first(self, index, trip):
        first = _queue(index, trip)
        other = index.create_trip(owner_id=trip.owner_id, name="Vienna", trip_id=new_id())
        index.enqueue_job(owner_id=trip.owner_id, trip_id=other.id, kind="build", job_id=new_id())
        claimed = index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        assert claimed.id == first

    def test_two_threads_claiming_at_once_never_get_the_same_job(self, tmp_path, index, trip):
        """The claim is one transaction, so this is a property of the store, not of timing.

        Each thread opens its **own** connection, because that is how a separate worker process
        would do it -- a shared connection would prove nothing about two processes.
        """
        trips = [trip] + [
            index.create_trip(owner_id=trip.owner_id, name=f"t{n}", trip_id=new_id())
            for n in range(5)
        ]
        for one in trips:
            index.enqueue_job(owner_id=one.owner_id, trip_id=one.id, kind="build", job_id=new_id())

        claimed: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(4)

        def claim_all() -> None:
            store = SqliteIndex(tmp_path / "index.db")
            start.wait()
            try:
                while True:
                    job = store.claim_next_job(
                        worker_id=str(threading.get_ident()), now=datetime.now(UTC)
                    )
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
            finally:
                store.close()

        threads = [threading.Thread(target=claim_all) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(claimed) == len(set(claimed)) == len(trips)


class TestOwnershipIsInTheQuery:
    def test_another_account_cannot_read_the_job(self, index, trip):
        job_id = _queue(index, trip)
        intruder = index.ensure_user(kind="email", value="someone@else.example")
        assert index.get_job(owner_id=intruder.id, job_id=job_id) is None

    def test_the_owner_can(self, index, trip):
        """The control. Without it the assertion above passes on a broken query."""
        job_id = _queue(index, trip)
        assert index.get_job(owner_id=trip.owner_id, job_id=job_id) is not None

    def test_another_account_sees_no_jobs_for_the_trip(self, index, trip):
        _queue(index, trip)
        intruder = index.ensure_user(kind="email", value="someone@else.example")
        assert index.list_jobs(owner_id=intruder.id, trip_id=trip.id) == []


class TestStaleness:
    def test_a_silent_worker_is_reported(self, index, trip):
        _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC) - timedelta(hours=2))
        stale = index.stale_running_jobs(before=datetime.now(UTC) - timedelta(minutes=5))
        assert [job.state for job in stale] == ["running"]

    def test_a_worker_that_just_reported_is_not(self, index, trip):
        """The control: staleness has to be able to say no."""
        _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        assert index.stale_running_jobs(before=datetime.now(UTC) - timedelta(minutes=5)) == []

    def test_requeueing_keeps_the_attempt_count(self, index, trip):
        """Attempts are not reset: the attempt happened, and the limit depends on remembering."""
        job_id = _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        index.requeue_job(job_id=job_id, at=datetime.now(UTC), error="worker died")
        job = index.get_job(owner_id=trip.owner_id, job_id=job_id)
        assert (job.state, job.attempts, job.worker_id) == ("queued", 1, "")

    def test_a_requeued_job_can_be_claimed_again(self, index, trip):
        job_id = _queue(index, trip)
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        index.requeue_job(job_id=job_id, at=datetime.now(UTC))
        again = index.claim_next_job(worker_id="w2", now=datetime.now(UTC))
        assert (again.id, again.attempts) == (job_id, 2)


class TestFinishing:
    def test_a_failed_job_must_carry_a_reason(self, index, trip):
        """A failure with no reason tells the client only that something broke."""
        job_id = _queue(index, trip)
        with pytest.raises(IndexError_, match="reason"):
            index.finish_job(job_id=job_id, state="failed", at=datetime.now(UTC))

    def test_a_state_that_is_not_terminal_is_refused(self, index, trip):
        job_id = _queue(index, trip)
        with pytest.raises(IndexError_, match="succeeded"):
            index.finish_job(job_id=job_id, state="running", at=datetime.now(UTC))


class TestQueuePosition:
    def test_the_position_counts_jobs_created_earlier(self, index, trip):
        ids = []
        for name in ("a", "b", "c"):
            one = index.create_trip(owner_id=trip.owner_id, name=name, trip_id=new_id())
            job, _ = index.enqueue_job(
                owner_id=one.owner_id, trip_id=one.id, kind="build", job_id=new_id()
            )
            ids.append(job.id)
        assert [index.queued_ahead(job_id=job_id) for job_id in ids] == [0, 1, 2]

    def test_a_claimed_job_no_longer_counts_as_ahead(self, index, trip):
        first, second = (
            index.create_trip(owner_id=trip.owner_id, name=n, trip_id=new_id()) for n in ("a", "b")
        )
        index.enqueue_job(owner_id=trip.owner_id, trip_id=first.id, kind="build", job_id=new_id())
        job, _ = index.enqueue_job(
            owner_id=trip.owner_id, trip_id=second.id, kind="build", job_id=new_id()
        )
        assert index.queued_ahead(job_id=job.id) == 1
        index.claim_next_job(worker_id="w1", now=datetime.now(UTC))
        assert index.queued_ahead(job_id=job.id) == 0
