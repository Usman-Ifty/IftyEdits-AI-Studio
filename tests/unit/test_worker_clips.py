"""Worker execution of clip-analysis jobs: success, retry idempotency, cancel, failure."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.models import ArtifactKind, JobStatus, Video
from iftyedits.core.protocols import MediaProber
from iftyedits.pipeline.jobs import ANALYZE_CLIPS, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.transcripts import TranscriptRepository
from tests.clip_db_fakes import add_artifact, save_transcript
from tests.clip_fakes import podcast, write_levels_wav
from tests.conftest import FakeProber, MakeMedia


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Show.mp4")).video


@pytest.fixture
def worker(settings: Settings) -> Iterator[Worker]:
    def prober(_: Settings) -> MediaProber:
        return FakeProber()

    w = Worker(
        settings,
        prober_factory=prober,
        worker_id="w-clips",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
    )
    yield w
    w.close()


def _with_transcript(settings: Settings, conn: sqlite3.Connection, video: Video) -> None:
    save_transcript(conn, video, podcast())
    root = settings.derived_dir.resolve() / f"v{video.id}"
    audio = write_levels_wav(root / "audio.wav", [(200.0, 0.1)], rate=2000)
    add_artifact(settings, conn, video, ArtifactKind.AUDIO_WAV, audio)


def test_clip_analysis_job_succeeds_and_retry_is_idempotent(
    worker: Worker, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _with_transcript(settings, db_conn, video)
    queue = JobQueue(db_conn)
    job_id, created = queue.enqueue_clip_analysis(video.id)
    assert created and queue.enqueue_clip_analysis(video.id) == (job_id, False)
    assert queue.active_clip_analysis(video.id) is not None
    assert worker.run(once=True) == 1

    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.SUCCEEDED and job.kind == ANALYZE_CLIPS
    assert job.result is not None and job.result["candidates"] >= 2
    assert job.message and "clip suggestions" in job.message
    assert queue.latest(ANALYZE_CLIPS, video.id) == job
    analysis = ClipRepository(db_conn).current(video.id)
    assert analysis is not None and analysis.job_id == job_id

    again, created = queue.enqueue_clip_analysis(video.id)
    assert created and again != job_id
    assert worker.run(once=True) == 1
    second = queue.get(again)
    assert second is not None and second.result is not None and second.result["reused"] is True
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 1

    forced, _ = queue.enqueue_clip_analysis(video.id, force=True)
    worker.run(once=True)
    third = queue.get(forced)
    assert third is not None and third.result is not None and third.result["reused"] is False
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 1
    assert len(ClipRepository(db_conn).candidates(third.result["analysis_id"])) >= 2


def test_job_without_a_transcript_fails_with_guidance(
    worker: Worker, db_conn: sqlite3.Connection, video: Video
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_clip_analysis(video.id)
    worker.run(once=True)
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
    assert job.error and "Transcribe it first" in job.error
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0


def test_cancelled_job_stores_nothing(
    worker: Worker, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _with_transcript(settings, db_conn, video)
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_clip_analysis(video.id)
    assert queue.request_cancel(job_id)
    worker.run(once=True)
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.CANCELLED
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0
    assert TranscriptRepository(db_conn).current(video.id) is not None  # transcript untouched
    assert Path(settings.originals_dir / video.vault_relpath).is_file()
