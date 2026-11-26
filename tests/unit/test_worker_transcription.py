"""Worker execution of transcription and model-download jobs (fake engine, no network)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import ModelDownloadError
from iftyedits.core.models import JobStatus, Video
from iftyedits.core.protocols import MediaProber, SpeechModelStore, SpeechTranscriber
from iftyedits.pipeline.jobs import DOWNLOAD_MODEL, TRANSCRIBE_VIDEO, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService
from iftyedits.storage.transcripts import TranscriptRepository
from tests.conftest import FakeProber, MakeMedia
from tests.transcription_fakes import FakeAudioPrep, FakeModelStore, FakeTranscriber


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Stream VOD.mp4")).video


@pytest.fixture
def engine() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def models(settings: Settings) -> FakeModelStore:
    return FakeModelStore(settings.models_dir)


def _build(settings: Settings, engine: FakeTranscriber, models: FakeModelStore) -> Worker:
    def prober(_: Settings) -> MediaProber:
        return FakeProber()

    def transcriber(_: Settings) -> SpeechTranscriber:
        return engine

    def store(_: Settings) -> SpeechModelStore:
        return models

    w = Worker(
        settings,
        prober_factory=prober,
        transcriber_factory=transcriber,
        model_store_factory=store,
        worker_id="w-main",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
    )
    w._media = FakeAudioPrep(settings, w.conn)  # type: ignore[assignment]
    return w


@pytest.fixture
def worker(settings: Settings, engine: FakeTranscriber, models: FakeModelStore) -> Iterator[Worker]:
    w = _build(settings, engine, models)
    yield w
    w.close()


def _job(db_conn: sqlite3.Connection, job_id: int) -> Any:
    job = JobQueue(db_conn).get(job_id)
    assert job is not None
    return job


def test_transcription_job_succeeds(
    worker: Worker, db_conn: sqlite3.Connection, video: Video
) -> None:
    queue = JobQueue(db_conn)
    job_id, created = queue.enqueue_transcription(video.id, model="small", language="en")
    assert created
    assert queue.enqueue_transcription(video.id, model="tiny", language=None) == (job_id, False)
    assert worker.process_one()
    job = _job(db_conn, job_id)
    assert job.status is JobStatus.SUCCEEDED, job.error
    assert job.result["segments"] == 2 and job.result["language"] == "en"
    assert job.message == "2 segments, 5 words, en (small)"
    transcript = TranscriptRepository(db_conn).current(video.id)
    assert transcript is not None and transcript.job_id == job_id
    assert queue.latest(TRANSCRIBE_VIDEO, video.id) == job
    assert queue.latest_by_video(TRANSCRIBE_VIDEO) == {video.id: job}
    # A second identical job reuses the stored transcript.
    again, _ = queue.enqueue_transcription(video.id, model="small", language="en")
    assert worker.process_one()
    assert _job(db_conn, again).result["reused"] is True


def test_missing_model_fails_with_instructions(
    worker: Worker, db_conn: sqlite3.Connection, video: Video, models: FakeModelStore
) -> None:
    models.installed.clear()
    job_id, _ = JobQueue(db_conn).enqueue_transcription(video.id, model="small", language=None)
    worker.process_one()
    job = _job(db_conn, job_id)
    assert job.status is JobStatus.FAILED
    assert "is not downloaded" in job.error and "models download small" in job.error


def test_engine_error_fails_job_and_retry_succeeds(
    worker: Worker, db_conn: sqlite3.Connection, video: Video, engine: FakeTranscriber
) -> None:
    queue = JobQueue(db_conn)
    engine.behaviour = "error"
    failed, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    worker.process_one()
    assert _job(db_conn, failed).status is JobStatus.FAILED
    assert "boom" in _job(db_conn, failed).error
    engine.behaviour = "ok"
    retry, created = queue.enqueue_transcription(video.id, model="small", language=None)
    assert created and retry != failed
    worker.process_one()
    assert _job(db_conn, retry).status is JobStatus.SUCCEEDED


def test_invalid_language_in_payload_fails_cleanly(
    worker: Worker, db_conn: sqlite3.Connection, video: Video
) -> None:
    job_id = JobQueue(db_conn).enqueue(
        TRANSCRIBE_VIDEO, {"video_id": video.id, "model": "small", "language": "zz"}
    )
    worker.process_one()
    job = _job(db_conn, job_id)
    assert job.status is JobStatus.FAILED and "Unsupported language" in job.error


def test_cancelling_a_running_transcription(
    db_conn: sqlite3.Connection,
    video: Video,
    engine: FakeTranscriber,
    models: FakeModelStore,
    settings: Settings,
) -> None:
    engine.behaviour = "wait_for_cancel"
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)

    def run() -> None:
        w = _build(settings, engine, models)  # SQLite connections stay in their thread
        try:
            w.process_one()
        finally:
            w.close()

    runner = threading.Thread(target=run)
    runner.start()
    assert engine.started.wait(10)
    assert queue.request_cancel(job_id).value == "requested"
    runner.join(20)
    assert not runner.is_alive()
    job = _job(db_conn, job_id)
    assert job.status is JobStatus.CANCELLED
    assert TranscriptRepository(db_conn).for_video(video.id) == []


def test_download_job_installs_model(
    worker: Worker, db_conn: sqlite3.Connection, models: FakeModelStore
) -> None:
    models.installed.clear()
    queue = JobQueue(db_conn)
    job_id, created = queue.enqueue_model_download("small")
    assert created and queue.enqueue_model_download("small") == (job_id, False)
    assert queue.active_download("small") is not None
    assert [j.id for j in queue.active_downloads()] == [job_id]
    worker.process_one()
    job = _job(db_conn, job_id)
    assert job.status is JobStatus.SUCCEEDED and job.result["state"] == "installed"
    assert job.message == "Model small installed"
    assert models.installs == ["small"] and "small" in models.installed
    assert queue.active_download("small") is None


def test_download_failures_are_reported(
    worker: Worker, db_conn: sqlite3.Connection, models: FakeModelStore
) -> None:
    models.fail_with = ModelDownloadError("Could not download model small: offline")
    queue = JobQueue(db_conn)
    failed, _ = queue.enqueue_model_download("small")
    unknown = queue.enqueue(DOWNLOAD_MODEL, {"model": "enormous"})
    worker.process_one()
    worker.process_one()
    assert "offline" in _job(db_conn, failed).error
    assert "Unknown speech model" in _job(db_conn, unknown).error
