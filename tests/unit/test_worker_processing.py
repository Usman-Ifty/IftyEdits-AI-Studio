"""Worker integration of processing jobs, cancellation and lost ownership (no FFmpeg)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import ExternalToolError, JobCancelledError, ProcessingError
from iftyedits.core.models import JobStatus, Stage
from iftyedits.core.protocols import CancelToken, MediaProber, MediaTranscoder
from iftyedits.pipeline.jobs import PROCESS_VIDEO, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.media_prep import OverallProgress, PrepareReport
from iftyedits.storage.db import open_database
from iftyedits.storage.originals import CHUNK_SIZE
from tests.conftest import FakeProber, MakeMedia


class FakeMedia:
    """Stands in for MediaPrepService; behaviour chosen per test."""

    def __init__(self, behaviour: str = "ok") -> None:
        self.behaviour = behaviour
        self.calls: list[dict[str, Any]] = []
        self.started = threading.Event()

    def prepare(
        self,
        video_id: int,
        stages: Sequence[Stage],
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: OverallProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> PrepareReport:
        self.calls.append({"video_id": video_id, "stages": list(stages), "force": force})
        self.started.set()
        if progress is not None:
            progress(0.5, "proxy: 50%")
        if self.behaviour == "wait_for_cancel":
            deadline = time.monotonic() + 20
            while cancel is not None and not cancel.is_set():
                if time.monotonic() > deadline:
                    raise AssertionError("cancel never arrived")
                time.sleep(0.01)
            raise JobCancelledError("Cancelled; FFmpeg was stopped")
        if self.behaviour == "fail":
            raise ProcessingError("1 stage(s) failed. proxy: ffmpeg failed (exit 1)")
        return PrepareReport(video_id)


def _worker(settings: Settings, **kwargs: Any) -> Worker:
    def prober(_: Settings) -> MediaProber:
        return FakeProber()

    return Worker(
        settings,
        prober_factory=prober,
        worker_id="w-main",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
        **kwargs,
    )


def _with_media(worker: Worker, media: FakeMedia) -> Worker:
    worker._media = media  # type: ignore[assignment]
    return worker


@pytest.fixture
def queue(db_conn: sqlite3.Connection) -> JobQueue:
    return JobQueue(db_conn)


def _run_in_thread(settings: Settings, media: FakeMedia) -> threading.Thread:
    """Run a worker in its own thread (SQLite connections belong to their thread)."""

    def run() -> None:
        worker = _with_media(_worker(settings), media)
        try:
            worker.run(once=True)
        finally:
            worker.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_processing_job_succeeds(settings: Settings, queue: JobQueue) -> None:
    job_id, _ = queue.enqueue_processing(3, [Stage.PROXY], force=True)
    media = FakeMedia()
    worker = _with_media(_worker(settings), media)
    try:
        assert worker.run(once=True) == 1
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.SUCCEEDED
    assert media.calls == [{"video_id": 3, "stages": [Stage.PROXY], "force": True}]


def test_processing_failure_is_recorded(settings: Settings, queue: JobQueue) -> None:
    job_id, _ = queue.enqueue_processing(3, [Stage.AUDIO])
    worker = _with_media(_worker(settings), FakeMedia("fail"))
    try:
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
    assert job.error is not None and "proxy: ffmpeg failed" in job.error


def test_cancelling_a_running_job_stops_it(settings: Settings, queue: JobQueue) -> None:
    job_id, _ = queue.enqueue_processing(3, [Stage.PROXY])
    media = FakeMedia("wait_for_cancel")
    thread = _run_in_thread(settings, media)
    assert media.started.wait(10)
    running = queue.get(job_id)
    assert running is not None and running.status is JobStatus.RUNNING
    assert queue.request_cancel(job_id).value == "requested"
    thread.join(timeout=20)
    assert not thread.is_alive()
    job = queue.get(job_id)
    assert job is not None
    assert (job.status, job.error) == (JobStatus.CANCELLED, "Cancelled by user")


def test_worker_that_lost_its_job_does_not_overwrite_it(
    settings: Settings, queue: JobQueue, db_conn: sqlite3.Connection
) -> None:
    job_id, _ = queue.enqueue_processing(3, [Stage.PROXY])
    media = FakeMedia("wait_for_cancel")
    thread = _run_in_thread(settings, media)
    assert media.started.wait(10)
    # Simulate stale recovery and another worker taking the job over.
    db_conn.execute(
        "UPDATE jobs SET worker_id = 'w-other', result = '{\"owner\": \"other\"}' WHERE id = ?",
        (job_id,),
    )
    thread.join(timeout=20)
    assert not thread.is_alive()
    job = queue.get(job_id)
    assert job is not None
    assert job.status is JobStatus.RUNNING and job.worker_id == "w-other"
    assert job.result == {"owner": "other"}


def test_missing_ffmpeg_fails_processing_job_clearly(settings: Settings, queue: JobQueue) -> None:
    def no_ffmpeg(_: Settings) -> MediaTranscoder:
        raise ExternalToolError("ffmpeg not found: 'ffmpeg'")

    job_id, _ = queue.enqueue_processing(1, [Stage.AUDIO])
    worker = _worker(settings, transcoder_factory=no_ffmpeg)
    try:
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
    assert job.error == "ffmpeg not found: 'ffmpeg'"


def test_import_with_process_flag_queues_preparation(
    settings: Settings, queue: JobQueue, make_media: MakeMedia
) -> None:
    (import_id,) = queue.enqueue_imports([make_media("talk.mp4")], process_after=True)
    media = FakeMedia()
    worker = _with_media(_worker(settings), media)
    try:
        assert worker.run(once=True) == 2
    finally:
        worker.close()
    imported = queue.get(import_id)
    assert imported is not None and imported.result is not None
    process_id = imported.result["process_job_id"]
    processed = queue.get(process_id)
    assert processed is not None
    assert (processed.kind, processed.status) == (PROCESS_VIDEO, JobStatus.SUCCEEDED)
    assert media.calls[0]["video_id"] == imported.result["video_id"]
    assert media.calls[0]["stages"] == [Stage.AUDIO, Stage.PROXY, Stage.THUMBNAIL]


def test_cancelling_an_import_removes_the_partial_copy(
    settings: Settings,
    queue: JobQueue,
    make_media: MakeMedia,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_media("big.mp4", body=b"\x01" * (CHUNK_SIZE * 4))
    (job_id,) = queue.enqueue_imports([source])
    worker = _worker(settings)
    original_import = worker.ingest.vault.import_file

    def cancel_then_import(path: Path, **kwargs: Any) -> Any:
        other = open_database(settings.db_path)
        try:
            JobQueue(other).request_cancel(job_id)
        finally:
            other.close()
        time.sleep(0.2)  # let the heartbeat thread notice
        return original_import(path, **kwargs)

    monkeypatch.setattr(worker.ingest.vault, "import_file", cancel_then_import)
    try:
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.CANCELLED
    assert not any(p.is_file() for p in settings.originals_dir.rglob("*"))
    assert not any(settings.staging_dir.glob("*.partial"))
    assert source.read_bytes()[:9] == b"FAKEVIDEO"


def test_startup_marks_interrupted_stages_and_cleans_old_partials(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    import os

    db_conn.execute(
        "INSERT INTO videos (sha256, vault_relpath, original_filename, size_bytes, status, "
        "imported_at) VALUES (?, 'ee/x.mp4', 'x.mp4', 1, 'ready', 'now')",
        ("e" * 64,),
    )
    db_conn.execute(
        "INSERT INTO processing_stages (video_id, stage, status, params_hash, params_json, "
        "started_at) VALUES (1, 'proxy', 'running', 'p', '{}', 'now')"
    )
    old = settings.derived_dir / "ee" / ".proxy.mp4.abc.partial"
    fresh = settings.derived_dir / "ee" / ".audio.wav.def.partial"
    keep = settings.derived_dir / "ee" / "proxy-720p-x.mp4"
    old.parent.mkdir(parents=True)
    for path in (old, fresh, keep):
        path.write_bytes(b"x")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(keep, (1_000_000_000, 1_000_000_000))

    worker = _worker(settings)
    try:
        worker.run(once=True)
    finally:
        worker.close()
    status = db_conn.execute("SELECT status FROM processing_stages").fetchone()[0]
    assert status == "failed"
    assert not old.exists()
    assert fresh.exists() and keep.exists()
