"""Production job outcomes: stage failure, cancellation, restart recovery (fake stages)."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import ExternalToolError, JobCancelledError, TranscriptionError
from iftyedits.core.models import JobStatus
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.production import (
    ClipOutcome,
    ProductionOptions,
    ProductionReport,
    ProductionService,
    Stages,
)
from tests.conftest import FakeProber
from tests.unit.test_production import FakeClips, FakeShorts, cand
from tests.unit.test_worker_url import Downloader


def _service(
    fail_stage: str | None = None,
    cancel_in: str | None = None,
    cancel: threading.Event | None = None,
    calls: list[str] | None = None,
) -> tuple[ProductionService, FakeShorts]:
    seen = calls if calls is not None else []
    shorts = FakeShorts()

    def stage(name: str) -> Any:
        def run(video_id: int, options: Any, progress: Any, token: Any) -> None:
            seen.append(name)
            if name == fail_stage:
                raise ExternalToolError(f"{name} exploded")
            if name == cancel_in and cancel is not None:
                cancel.set()
                raise JobCancelledError(f"cancelled in {name}")

        return run

    stages = Stages(lambda *_: 7, stage("prepare"), stage("transcribe"), stage("analyze"))
    return ProductionService(stages, shorts, FakeClips([cand(1, 90), cand(2, 80)])), shorts  # type: ignore[arg-type]


@pytest.mark.parametrize("stage", ["prepare", "transcribe", "analyze"])
def test_a_failing_stage_stops_everything_after_it(stage: str) -> None:
    calls: list[str] = []
    service, shorts = _service(fail_stage=stage, calls=calls)
    with pytest.raises(ExternalToolError, match=f"{stage} exploded"):
        service.run(url=None, video_id=7, options=ProductionOptions(), progress=lambda *_: None)
    order = ["prepare", "transcribe", "analyze"]
    assert calls == order[: order.index(stage) + 1]  # nothing ran after the failure
    assert shorts.rendered == []


@pytest.mark.parametrize("stage", ["prepare", "transcribe", "analyze"])
def test_cancel_inside_a_stage_renders_nothing(stage: str) -> None:
    token = threading.Event()
    service, shorts = _service(cancel_in=stage, cancel=token)
    with pytest.raises(JobCancelledError):
        service.run(
            url=None,
            video_id=7,
            options=ProductionOptions(),
            progress=lambda *_: None,
            cancel=token,
        )
    assert shorts.rendered == []


def _worker(settings: Settings) -> Worker:
    return Worker(
        settings,
        prober_factory=lambda _: FakeProber(),
        downloader_factory=lambda _: Downloader(),
        worker_id="w-prod",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
    )


def _run_job(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, run: Any
) -> Any:
    worker = _worker(settings)
    try:
        monkeypatch.setattr(worker, "production", lambda: SimpleNamespace(run=run))
        queue = JobQueue(conn)
        job_id, _ = queue.enqueue_production({}, video_id=1)
        worker.process_one()
        return queue.get(job_id)
    finally:
        worker.close()


def test_stage_failure_is_a_failed_job_with_the_reason(
    settings: Settings, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(**_: Any) -> Any:
        raise TranscriptionError("The speech model is not installed.")

    job = _run_job(settings, db_conn, monkeypatch, run)
    assert job.status is JobStatus.FAILED
    assert "speech model is not installed" in (job.error or "")


def test_cancelled_production_is_cancelled_not_failed(
    settings: Settings, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(**_: Any) -> Any:
        raise JobCancelledError("Cancelled")

    job = _run_job(settings, db_conn, monkeypatch, run)
    assert job.status is JobStatus.CANCELLED and not job.result


def test_partial_success_is_a_successful_job_that_names_the_failure(
    settings: Settings, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = ClipOutcome(1, "k1", "t", 70.0, 0.0, 20.0, "succeeded", output="/e/a.mp4")
    bad = ClipOutcome(2, "k2", "t", 60.0, 0.0, 20.0, "failed", error="encoder exploded")
    report = ProductionReport(1, (ok, bad), 4)
    job = _run_job(settings, db_conn, monkeypatch, lambda **_: report)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None and job.result["succeeded"] == 1 and job.result["failed"] == 1
    assert "1 failed" in (job.message or "")


def test_interrupted_production_is_requeued_and_finishes_once(
    settings: Settings, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_production({}, video_id=1)
    queue.claim("dead-worker")  # the worker died after claiming the job
    db_conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
    report = ProductionReport(
        1, (ClipOutcome(1, "k", "t", 70.0, 0.0, 20.0, "succeeded", output="/e/a.mp4"),), 1
    )
    runs: list[int] = []

    def run(**_: Any) -> Any:
        runs.append(1)
        return report

    worker = _worker(settings)
    try:
        monkeypatch.setattr(worker, "production", lambda: SimpleNamespace(run=run))
        worker.run(once=True)
    finally:
        worker.close()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2 and runs == [1]


def test_production_that_keeps_killing_its_worker_ends_failed(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_production({}, video_id=1)
    for _ in range(3):
        queue.claim("dead-worker")
        db_conn.execute("UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
        queue.recover_stale(60, max_attempts=3)
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED and job.error


def test_two_workers_never_claim_the_same_production_job(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    from iftyedits.storage.db import open_database

    queue = JobQueue(db_conn)
    ids = {queue.enqueue_production({"n": n}, video_id=n)[0] for n in range(1, 13)}
    claimed: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def take(name: str) -> None:
        conn = open_database(settings.db_path)  # one connection per worker, as in production
        try:
            mine = JobQueue(conn)
            barrier.wait()
            while (job := mine.claim(name)) is not None:
                with lock:
                    claimed.append(job.id)
        finally:
            conn.close()

    threads = [threading.Thread(target=take, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(claimed) == sorted(ids)  # each job exactly once, none lost
