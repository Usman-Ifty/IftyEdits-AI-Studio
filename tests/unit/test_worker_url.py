"""Worker execution of ``fetch_url`` jobs (fake downloader, fake prober, no network)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core.errors import SourceDownloadError
from iftyedits.core.models import JobStatus
from iftyedits.core.protocols import MediaProber, SourceDownloader
from iftyedits.core.sources import SourceMetadata, VideoUrl
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from tests.conftest import FAKE_VIDEO, FakeProber

URL = "https://www.youtube.com/watch?v=abcdefghijk"


class Downloader:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.downloads = 0

    def fetch_metadata(self, url: VideoUrl) -> SourceMetadata:
        return SourceMetadata(
            url.source_key, url.canonical, "A Title", "Me", 60.0, None, None, False
        )

    def download(
        self,
        url: VideoUrl,
        directory: Path,
        *,
        progress: object = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        self.downloads += 1
        if self.fail:
            raise SourceDownloadError("This video is private.")
        target = directory / "raw.mp4"
        target.write_bytes(FAKE_VIDEO + b"x" * 2048)
        return target


def _worker(settings: Settings, downloader: SourceDownloader) -> Worker:
    def prober(_: Settings) -> MediaProber:
        return FakeProber()

    return Worker(
        settings,
        prober_factory=prober,
        downloader_factory=lambda _: downloader,
        worker_id="w-url",
        heartbeat_interval_s=0.01,
        poll_interval_s=0.01,
    )


def test_fetch_job_downloads_once_and_reuses(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    downloader = Downloader()
    worker = _worker(settings, downloader)
    try:
        queue = JobQueue(db_conn)
        first, created = queue.enqueue_url_fetch(URL)
        assert created
        assert queue.enqueue_url_fetch(URL) == (first, False)
        worker.process_one()
        job = queue.get(first)
        assert job is not None and job.status is JobStatus.SUCCEEDED
        assert job.result and job.result["reused"] is False
        second, _ = queue.enqueue_url_fetch(URL)
        worker.process_one()
        again = queue.get(second)
        assert again is not None and again.result and again.result["reused"] is True
        assert downloader.downloads == 1
        assert not list(settings.staging_dir.glob("url-*"))
    finally:
        worker.close()


def test_fetch_job_failure_is_readable_and_leaves_no_staging(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    worker = _worker(settings, Downloader(fail=True))
    try:
        queue = JobQueue(db_conn)
        job_id, _ = queue.enqueue_url_fetch(URL)
        worker.process_one()
        job = queue.get(job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert "private" in (job.error or "")
        assert not list(settings.staging_dir.glob("url-*"))
    finally:
        worker.close()
