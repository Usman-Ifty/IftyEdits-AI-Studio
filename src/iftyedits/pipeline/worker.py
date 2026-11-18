"""Background worker process. Runs heavy jobs outside the Streamlit request lifecycle."""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.config import Settings
from iftyedits.core.errors import IftyEditsError, ImportRecordError
from iftyedits.core.models import Job
from iftyedits.core.protocols import MediaProber
from iftyedits.pipeline.jobs import IMPORT_FILE, JobQueue
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.storage.db import open_database
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

ProberFactory = Callable[[Settings], MediaProber]


def default_prober(settings: Settings) -> MediaProber:
    """ffprobe configured from settings."""
    return FFprobe(settings.ffprobe_path, settings.probe_timeout_s)


@dataclass(slots=True)
class _Progress:
    fraction: float = 0.0
    message: str | None = None


class Worker:
    """Claims queued jobs one at a time and executes them."""

    def __init__(
        self,
        settings: Settings,
        *,
        prober_factory: ProberFactory = default_prober,
        worker_id: str | None = None,
        poll_interval_s: float = 1.0,
        heartbeat_interval_s: float = 2.0,
        stale_after_s: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval_s = poll_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self.max_attempts = max_attempts
        self.conn = open_database(settings.db_path)
        self.queue = JobQueue(self.conn)
        self.ingest = IngestService(
            settings, VideoRepository(self.conn), prober_factory(settings), build_vault(settings)
        )

    def run(self, stop: threading.Event | None = None, *, once: bool = False) -> int:
        """Process jobs until ``stop`` is set (or the queue is empty with ``once``)."""
        stop = stop or threading.Event()
        processed = 0
        self.queue.register_worker(self.worker_id, os.getpid())
        try:
            self.ingest.vault.clean_staging()
            recovered = self.queue.recover_stale(self.stale_after_s, self.max_attempts)
            if recovered:
                logger.warning("Recovered %d interrupted job(s)", recovered)
            while not stop.is_set():
                if self.process_one():
                    processed += 1
                    continue
                if once:
                    break
                self.queue.touch_worker(self.worker_id)
                stop.wait(self.poll_interval_s)
        finally:
            self.queue.remove_worker(self.worker_id)
        return processed

    def process_one(self) -> bool:
        """Claim and run one job. Returns False if the queue was empty."""
        job = self.queue.claim(self.worker_id)
        if job is None:
            return False
        logger.info("Job %d (%s) started", job.id, job.kind)
        progress = _Progress()
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(job.id, progress, done))
        beat.start()
        try:
            result, message = self._execute(job, progress)
        except ImportRecordError as exc:
            self.queue.fail(job.id, f"{exc} [sha256={exc.sha256}]")
        except IftyEditsError as exc:
            self.queue.fail(job.id, str(exc))
        except Exception as exc:
            logger.exception("Job %d crashed", job.id)
            self.queue.fail(job.id, f"Unexpected error: {type(exc).__name__}: {exc}")
        except BaseException:
            self.queue.requeue(job.id)
            raise
        else:
            self.queue.succeed(job.id, result, message)
        finally:
            done.set()
            beat.join()
        return True

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def _execute(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        if job.kind != IMPORT_FILE:
            raise IftyEditsError(f"Unknown job kind: {job.kind}")
        source = Path(str(job.payload["path"]))
        progress.message = f"Importing {source.name}"

        def on_progress(copied: int, total: int) -> None:
            progress.fraction = copied / total if total else 1.0

        outcome = self.ingest.import_video(source, progress=on_progress)
        video = outcome.video
        result = {
            "video_id": video.id,
            "sha256": video.sha256,
            "outcome": outcome.outcome.value,
            "status": video.status.value,
        }
        return result, f"{outcome.outcome.value}: {source.name}"

    def _heartbeat_loop(self, job_id: int, progress: _Progress, done: threading.Event) -> None:
        conn = open_database(self.settings.db_path)
        queue = JobQueue(conn)
        try:
            while True:
                try:
                    queue.heartbeat(job_id, progress.fraction, progress.message)
                    queue.touch_worker(self.worker_id)
                except sqlite3.Error:
                    logger.warning("Heartbeat write failed for job %d", job_id, exc_info=True)
                if done.wait(self.heartbeat_interval_s):
                    break
        finally:
            conn.close()
