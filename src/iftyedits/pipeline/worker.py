"""Background worker process. Runs heavy jobs outside the Streamlit request lifecycle.

One job runs at a time. A heartbeat thread records progress, notices cancellation
requests and detects lost ownership (another worker recovered the job); either one
sets the job's cancel flag so the running stage stops cooperatively.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iftyedits.adapters.faster_whisper import FasterWhisperTranscriber
from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.adapters.model_store import build_model_store
from iftyedits.config import Settings
from iftyedits.core.errors import (
    IftyEditsError,
    ImportRecordError,
    JobCancelledError,
    JobOwnershipError,
)
from iftyedits.core.models import ALL_STAGES, Job, Stage
from iftyedits.core.protocols import (
    MediaProber,
    MediaTranscoder,
    SpeechModelStore,
    SpeechTranscriber,
)
from iftyedits.pipeline.jobs import (
    DOWNLOAD_MODEL,
    IMPORT_FILE,
    PROCESS_VIDEO,
    TRANSCRIBE_VIDEO,
    JobQueue,
)
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.services.media_prep import MediaPrepService, clean_partials
from iftyedits.services.transcription import TranscriptionService, options_from_settings
from iftyedits.storage.db import open_database
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

ProberFactory = Callable[[Settings], MediaProber]
TranscoderFactory = Callable[[Settings], MediaTranscoder]
TranscriberFactory = Callable[[Settings], SpeechTranscriber]
ModelStoreFactory = Callable[[Settings], SpeechModelStore]


def default_prober(settings: Settings) -> MediaProber:
    """ffprobe configured from settings."""
    return FFprobe(settings.ffprobe_path, settings.probe_timeout_s)


def default_transcoder(settings: Settings) -> MediaTranscoder:
    """ffmpeg configured from settings. Raises ``ExternalToolError`` if missing."""
    return FFmpeg(settings.ffmpeg_path, settings.ffmpeg_timeout_s, encoder=settings.proxy_encoder)


def default_transcriber(settings: Settings) -> SpeechTranscriber:
    """faster-whisper on the CPU (imported lazily on first use)."""
    return FasterWhisperTranscriber()


def default_model_store(settings: Settings) -> SpeechModelStore:
    """Speech models under ``IFTYEDITS_MODELS_DIR``, never inside the originals vault."""
    return build_model_store(settings)


@dataclass(slots=True)
class _Progress:
    fraction: float = 0.0
    message: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    lost: threading.Event = field(default_factory=threading.Event)


class Worker:
    """Claims queued jobs one at a time and executes them."""

    def __init__(
        self,
        settings: Settings,
        *,
        prober_factory: ProberFactory = default_prober,
        transcoder_factory: TranscoderFactory = default_transcoder,
        transcriber_factory: TranscriberFactory = default_transcriber,
        model_store_factory: ModelStoreFactory = default_model_store,
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
        self.processing = ProcessingRepository(self.conn)
        self.prober = prober_factory(settings)
        self.transcoder_factory = transcoder_factory
        self.transcriber_factory = transcriber_factory
        self.models = model_store_factory(settings)
        self._media: MediaPrepService | None = None
        self._transcription: TranscriptionService | None = None
        self.ingest = IngestService(
            settings, VideoRepository(self.conn), self.prober, build_vault(settings)
        )

    def media(self) -> MediaPrepService:
        """Media preparation service, created on first use (needs ffmpeg)."""
        if self._media is None:
            self._media = MediaPrepService(
                self.settings,
                VideoRepository(self.conn),
                self.processing,
                self.transcoder_factory(self.settings),
                self.prober,
            )
        return self._media

    def transcription(self) -> TranscriptionService:
        """Transcription service, created on first use (the engine loads lazily)."""
        if self._transcription is None:
            self._transcription = TranscriptionService(
                self.settings,
                VideoRepository(self.conn),
                self.processing,
                TranscriptRepository(self.conn),
                self.transcriber_factory(self.settings),
                self.models,
                self.media,
            )
        return self._transcription

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
            orphaned = self.processing.fail_orphaned_running()
            if orphaned:
                logger.warning("Marked %d interrupted stage(s) as failed", orphaned)
            clean_partials(self.settings.derived_dir)
            self.models.clean_staging()
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
        owner = self.worker_id
        try:
            result, message = self._execute(job, progress)
        except JobOwnershipError as exc:
            logger.warning("Job %d: %s; result discarded", job.id, exc)
        except JobCancelledError:
            if progress.lost.is_set():
                logger.warning("Job %d stopped: another worker took it over", job.id)
            elif self.queue.mark_cancelled(job.id, worker_id=owner):
                logger.info("Job %d cancelled", job.id)
        except ImportRecordError as exc:
            self._fail(job.id, f"{exc} [sha256={exc.sha256}]")
        except IftyEditsError as exc:
            self._fail(job.id, str(exc))
        except Exception as exc:
            logger.exception("Job %d crashed", job.id)
            self._fail(job.id, f"Unexpected error: {type(exc).__name__}: {exc}")
        except BaseException:
            self.queue.requeue(job.id, worker_id=owner)
            raise
        else:
            if not self.queue.succeed(job.id, result, message, worker_id=owner):
                logger.warning("Job %d finished but is no longer owned; result discarded", job.id)
        finally:
            done.set()
            beat.join()
        return True

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    # ------------------------------------------------------------------ jobs

    def _fail(self, job_id: int, error: str) -> None:
        if not self.queue.fail(job_id, error, worker_id=self.worker_id):
            logger.warning("Job %d failed but is no longer owned: %s", job_id, error)

    def _execute(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        if job.kind == IMPORT_FILE:
            return self._import(job, progress)
        if job.kind == PROCESS_VIDEO:
            return self._process(job, progress)
        if job.kind == TRANSCRIBE_VIDEO:
            return self._transcribe(job, progress)
        if job.kind == DOWNLOAD_MODEL:
            return self._download_model(job, progress)
        raise IftyEditsError(f"Unknown job kind: {job.kind}")

    def _import(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        source = Path(str(job.payload["path"]))
        progress.message = f"Importing {source.name}"

        def on_progress(copied: int, total: int) -> None:
            if progress.cancel.is_set():
                raise JobCancelledError("Import cancelled; the partial copy was removed")
            progress.fraction = copied / total if total else 1.0

        outcome = self.ingest.import_video(source, progress=on_progress)
        video = outcome.video
        result: dict[str, Any] = {
            "video_id": video.id,
            "sha256": video.sha256,
            "outcome": outcome.outcome.value,
            "status": video.status.value,
        }
        message = f"{outcome.outcome.value}: {source.name}"
        if job.payload.get("process"):
            process_id, created = self.queue.enqueue_processing(video.id, ALL_STAGES)
            result["process_job_id"] = process_id
            message += f"; media preparation {'queued' if created else 'already queued'}"
        return result, message

    def _process(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        video_id = job.video_id
        if video_id is None:
            raise IftyEditsError("Processing job has no video_id")
        stages = [Stage(s) for s in job.payload.get("stages", [s.value for s in ALL_STAGES])]
        progress.message = "Preparing media"

        def on_progress(fraction: float, message: str) -> None:
            progress.fraction = fraction
            progress.message = message

        report = self.media().prepare(
            video_id,
            stages,
            force=bool(job.payload.get("force", False)),
            job_id=job.id,
            worker_id=self.worker_id,
            progress=on_progress,
            cancel=progress.cancel,
        )
        return report.as_result(), report.summary()

    def _transcribe(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        video_id = job.video_id
        if video_id is None:
            raise IftyEditsError("Transcription job has no video_id")
        model = job.payload.get("model")
        language = job.payload.get("language")
        options = options_from_settings(
            self.settings,
            model=str(model) if model else None,
            language=str(language) if language else "auto",
        )
        progress.message = f"Transcribing with {options.model}"

        def on_progress(fraction: float, message: str) -> None:
            progress.fraction = fraction
            progress.message = message

        report = self.transcription().transcribe(
            video_id,
            options,
            force=bool(job.payload.get("force", False)),
            job_id=job.id,
            worker_id=self.worker_id,
            progress=on_progress,
            cancel=progress.cancel,
        )
        return report.as_result(), report.summary()

    def _download_model(self, job: Job, progress: _Progress) -> tuple[dict[str, Any], str]:
        name = str(job.payload.get("model", ""))
        spec = self.models.spec(name)
        progress.message = f"Downloading {name}"

        def on_bytes(done: int, total: int) -> None:
            progress.fraction = done / total if total else 1.0
            progress.message = (
                f"Downloading {name}: {done / 1024**2:,.0f} of {total / 1024**2:,.0f} MB"
            )

        status = self.models.install(name, progress=on_bytes, cancel=progress.cancel)
        result = {
            "model": name,
            "state": status.state.value,
            "size_bytes": spec.size_bytes,
            "revision": spec.revision,
        }
        return result, f"Model {name} installed"

    def _heartbeat_loop(self, job_id: int, progress: _Progress, done: threading.Event) -> None:
        conn = open_database(self.settings.db_path)
        queue = JobQueue(conn)
        try:
            while True:
                try:
                    owned = queue.heartbeat(
                        job_id, progress.fraction, progress.message, worker_id=self.worker_id
                    )
                    queue.touch_worker(self.worker_id)
                    if not owned and not done.is_set():
                        progress.lost.set()
                        progress.cancel.set()
                    elif queue.cancel_requested(job_id):
                        progress.cancel.set()
                except sqlite3.Error:
                    logger.warning("Heartbeat write failed for job %d", job_id, exc_info=True)
                if done.wait(self.heartbeat_interval_s):
                    break
        finally:
            conn.close()
