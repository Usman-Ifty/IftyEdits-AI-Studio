"""Job queue and background worker."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.core.protocols import MediaProber
from iftyedits.pipeline.jobs import IMPORT_FILE, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.storage.originals import PARTIAL_SUFFIX
from iftyedits.storage.videos import VideoRepository
from tests.conftest import FakeProber, MakeMedia


@pytest.fixture
def queue(db_conn: sqlite3.Connection) -> JobQueue:
    return JobQueue(db_conn)


def _worker(settings: Settings, **kwargs: object) -> Worker:
    def factory(_: Settings) -> MediaProber:
        return FakeProber()

    return Worker(
        settings,
        prober_factory=factory,
        worker_id="test-worker",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
        **kwargs,  # type: ignore[arg-type]
    )


def test_enqueue_and_claim_in_order(queue: JobQueue) -> None:
    ids = queue.enqueue_imports([Path("/a.mp4"), Path("/b.mp4")])
    first = queue.claim("w1")
    second = queue.claim("w2")
    assert first is not None and second is not None
    assert [first.id, second.id] == ids
    assert first.status is JobStatus.RUNNING
    assert first.attempts == 1
    assert first.payload == {"path": "/a.mp4"}
    assert queue.claim("w3") is None


def test_enqueue_skips_paths_already_active(queue: JobQueue) -> None:
    assert len(queue.enqueue_imports([Path("/a.mp4")])) == 1
    assert queue.enqueue_imports([Path("/a.mp4"), Path("/b.mp4")]) != []
    assert queue.count_active() == 2
    job = queue.claim("w")
    assert job is not None
    queue.succeed(job.id, {"ok": True})
    assert len(queue.enqueue_imports([Path("/a.mp4")])) == 1


def test_lifecycle_updates(queue: JobQueue) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/x.mp4"})
    queue.claim("w")
    queue.heartbeat(job_id, 1.7, "copying")
    job = queue.get(job_id)
    assert job is not None
    assert job.progress == 1.0
    assert job.message == "copying"
    queue.fail(job_id, "boom")
    job = queue.get(job_id)
    assert job is not None
    assert (job.status, job.error) == (JobStatus.FAILED, "boom")
    assert queue.count_active() == 0


def test_recover_stale_requeues_then_fails(queue: JobQueue, db_conn: sqlite3.Connection) -> None:
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/x.mp4"})
    queue.claim("w")
    db_conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
    assert queue.recover_stale(60, max_attempts=2) == 1
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.QUEUED

    queue.claim("w")
    db_conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
    assert queue.recover_stale(60, max_attempts=2) == 1
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED


def test_recover_stale_ignores_live_jobs(queue: JobQueue) -> None:
    queue.enqueue(IMPORT_FILE, {"path": "/x.mp4"})
    queue.claim("w")
    assert queue.recover_stale(60, max_attempts=3) == 0


def test_worker_registry(queue: JobQueue) -> None:
    queue.register_worker("w1", 123)
    assert queue.live_workers() == ["w1"]
    queue.remove_worker("w1")
    assert queue.live_workers() == []


def test_worker_imports_queued_file(
    settings: Settings, queue: JobQueue, db_conn: sqlite3.Connection, make_media: MakeMedia
) -> None:
    source = make_media("talk.mp4")
    (job_id,) = queue.enqueue_imports([source])

    worker = _worker(settings)
    try:
        assert worker.run(once=True) == 1
    finally:
        worker.close()

    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED, job.error
    assert job.progress == 1.0
    assert job.result is not None and job.result["outcome"] == "imported"
    video = VideoRepository(db_conn).get(int(job.result["video_id"]))
    assert video is not None and video.original_filename == "talk.mp4"
    assert queue.live_workers() == []


def test_worker_marks_bad_input_failed(
    settings: Settings, queue: JobQueue, make_media: MakeMedia
) -> None:
    (job_id,) = queue.enqueue_imports([make_media("broken.mp4", kind=b"junk")])
    worker = _worker(settings)
    try:
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error is not None and "Not a readable media file" in job.error


def test_worker_unknown_kind_fails(settings: Settings, queue: JobQueue) -> None:
    job_id = queue.enqueue("transcode_everything", {})
    worker = _worker(settings)
    try:
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED


def test_worker_interrupt_requeues_job(
    settings: Settings,
    queue: JobQueue,
    make_media: MakeMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (job_id,) = queue.enqueue_imports([make_media("long.mp4")])
    worker = _worker(settings)

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.ingest, "import_video", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.QUEUED


def test_worker_startup_cleans_stale_partials_and_recovers_jobs(
    settings: Settings, queue: JobQueue, db_conn: sqlite3.Connection, make_media: MakeMedia
) -> None:
    import os

    settings.staging_dir.mkdir(parents=True)
    partial = settings.staging_dir / f"crashed{PARTIAL_SUFFIX}"
    partial.write_bytes(b"half")
    os.utime(partial, (1_000_000_000, 1_000_000_000))
    (job_id,) = queue.enqueue_imports([make_media("resume.mp4")])
    queue.claim("dead-worker")
    db_conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")

    worker = _worker(settings)
    try:
        worker.run(once=True)
    finally:
        worker.close()

    assert not partial.exists()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2


def test_worker_stops_on_event(settings: Settings) -> None:
    stop = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        worker = _worker(settings)
        try:
            worker.run(stop)
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker.close()

    thread = threading.Thread(target=run)
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
