"""Job states, cancellation, worker ownership and concurrent claiming (Phase 1B)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from iftyedits.core.models import ALL_STAGES, CancelOutcome, JobStatus, Stage
from iftyedits.pipeline.jobs import IMPORT_FILE, PROCESS_VIDEO, JobQueue
from iftyedits.storage.db import open_database

STALE = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def queue(db_conn: sqlite3.Connection) -> JobQueue:
    return JobQueue(db_conn)


def _status(queue: JobQueue, job_id: int) -> JobStatus:
    job = queue.get(job_id)
    assert job is not None
    return job.status


def test_cancel_queued_job_is_immediate_and_never_claimed(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    assert queue.request_cancel(job_id) is CancelOutcome.CANCELLED
    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.CANCELLED
    assert job.status.is_terminal
    assert job.finished_at is not None and job.cancel_requested_at is not None
    assert queue.claim("w") is None
    assert queue.count_active() == 0


def test_cancel_running_job_is_cooperative(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    queue.claim("w1")
    assert queue.request_cancel(job_id) is CancelOutcome.REQUESTED
    assert queue.request_cancel(job_id) is CancelOutcome.REQUESTED  # idempotent
    assert _status(queue, job_id) is JobStatus.RUNNING
    assert queue.cancel_requested(job_id)

    assert not queue.mark_cancelled(job_id, worker_id="someone-else")
    assert queue.mark_cancelled(job_id, worker_id="w1")
    job = queue.get(job_id)
    assert job is not None
    assert (job.status, job.error) == (JobStatus.CANCELLED, "Cancelled by user")


def test_cancel_finished_or_unknown_job(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    queue.claim("w")
    assert queue.succeed(job_id, {"ok": True}, worker_id="w")
    assert queue.request_cancel(job_id) is CancelOutcome.NOT_ACTIVE
    assert _status(queue, job_id) is JobStatus.SUCCEEDED
    assert queue.request_cancel(9999) is CancelOutcome.NOT_FOUND


def test_only_owner_can_update_running_job(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    queue.claim("w1")
    assert not queue.heartbeat(job_id, 0.5, "x", worker_id="w2")
    assert not queue.succeed(job_id, {"from": "w2"}, worker_id="w2")
    assert not queue.fail(job_id, "w2 failed", worker_id="w2")
    assert _status(queue, job_id) is JobStatus.RUNNING
    assert queue.heartbeat(job_id, 0.5, "copying", worker_id="w1")
    assert queue.succeed(job_id, {"from": "w1"}, worker_id="w1")
    job = queue.get(job_id)
    assert job is not None and job.result == {"from": "w1"}


def test_stale_worker_cannot_overwrite_new_owner(
    queue: JobQueue, db_conn: sqlite3.Connection
) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    queue.claim("old")
    db_conn.execute("UPDATE jobs SET heartbeat_at = ?", (STALE,))
    assert queue.recover_stale(60, max_attempts=3) == 1
    new = queue.claim("new")
    assert new is not None and new.worker_id == "new" and new.attempts == 2

    assert not queue.heartbeat(job_id, 0.9, worker_id="old")
    assert not queue.fail(job_id, "old worker woke up", worker_id="old")
    assert not queue.succeed(job_id, {"from": "old"}, worker_id="old")
    assert queue.succeed(job_id, {"from": "new"}, worker_id="new")
    job = queue.get(job_id)
    assert job is not None and job.result == {"from": "new"} and job.error is None


def test_finished_jobs_cannot_change_state(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    assert not queue.succeed(job_id, {})  # queued, not running
    assert not queue.fail(job_id, "x")
    queue.claim("w")
    assert queue.fail(job_id, "boom", worker_id="w")
    assert not queue.succeed(job_id, {"late": True}, worker_id="w")
    assert not queue.mark_cancelled(job_id, worker_id="w")
    assert not queue.requeue(job_id, worker_id="w")
    job = queue.get(job_id)
    assert job is not None and (job.status, job.error) == (JobStatus.FAILED, "boom")


def test_recover_stale_cancels_jobs_with_pending_cancellation(
    queue: JobQueue, db_conn: sqlite3.Connection
) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    queue.claim("w")
    queue.request_cancel(job_id)
    db_conn.execute("UPDATE jobs SET heartbeat_at = ?", (STALE,))
    assert queue.recover_stale(60, max_attempts=3) == 1
    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.CANCELLED and job.worker_id is None


def test_requeue_honours_cancellation(queue: JobQueue) -> None:
    keep = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    stop = queue.enqueue(IMPORT_FILE, {"path": "/b.mp4"})
    queue.claim("w")
    queue.claim("w")
    queue.request_cancel(stop)
    assert queue.requeue(keep, worker_id="w")
    assert queue.requeue(stop, worker_id="w")
    assert _status(queue, keep) is JobStatus.QUEUED
    assert _status(queue, stop) is JobStatus.CANCELLED


def test_concurrent_claims_never_share_a_job(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    queue = JobQueue(db_conn)
    expected = {queue.enqueue(IMPORT_FILE, {"path": f"/{i}.mp4"}) for i in range(40)}
    db_path = Path(db_conn.execute("PRAGMA database_list").fetchone()["file"])
    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def claim_all(name: str) -> None:
        conn = open_database(db_path)
        try:
            q = JobQueue(conn)
            start.wait()
            while (job := q.claim(name)) is not None:
                with lock:
                    claimed.append((name, job.id))
        finally:
            conn.close()

    threads = [threading.Thread(target=claim_all, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ids = [job_id for _, job_id in claimed]
    assert sorted(ids) == sorted(expected)  # every job exactly once
    rows = db_conn.execute("SELECT id, worker_id, attempts FROM jobs").fetchall()
    owners = dict(claimed)
    assert all(r["attempts"] == 1 for r in rows)
    assert {r["worker_id"] for r in rows} <= set(owners)


def test_enqueue_processing_deduplicates_active_jobs(queue: JobQueue) -> None:
    first, created = queue.enqueue_processing(7, ALL_STAGES)
    assert created
    again, created_again = queue.enqueue_processing(7, [Stage.PROXY], force=True)
    assert (again, created_again) == (first, False)
    job = queue.get(first)
    assert job is not None
    assert job.kind == PROCESS_VIDEO and job.video_id == 7
    assert job.payload == {"video_id": 7, "stages": ["audio", "proxy", "thumbnail"], "force": False}
    assert queue.active_processing(7) == job
    assert queue.active_processing(8) is None

    queue.request_cancel(first)
    _, created_after_cancel = queue.enqueue_processing(7, ALL_STAGES)
    assert created_after_cancel
    with pytest.raises(ValueError, match="stage"):
        queue.enqueue_processing(7, [])


def test_enqueue_imports_can_request_processing(queue: JobQueue) -> None:
    (plain,) = queue.enqueue_imports([Path("/a.mp4")])
    (prepared,) = queue.enqueue_imports([Path("/b.mp4")], process_after=True)
    plain_job, prepared_job = queue.get(plain), queue.get(prepared)
    assert plain_job is not None and plain_job.payload == {"path": "/a.mp4"}
    assert prepared_job is not None and prepared_job.payload == {"path": "/b.mp4", "process": True}


def test_list_active_oldest_first(queue: JobQueue) -> None:
    a = queue.enqueue(IMPORT_FILE, {"path": "/a.mp4"})
    b = queue.enqueue(IMPORT_FILE, {"path": "/b.mp4"})
    c = queue.enqueue(IMPORT_FILE, {"path": "/c.mp4"})
    queue.request_cancel(b)
    assert [j.id for j in queue.list_active()] == [a, c]
