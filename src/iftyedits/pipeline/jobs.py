"""SQLite-backed job queue shared by the UI (enqueue, cancel) and the worker (execute).

State machine (enforced in SQL, so concurrent processes cannot break it)::

    queued  --claim-->            running
    queued  --request_cancel-->   cancelled
    running --succeed/fail-->     succeeded / failed      (owning worker only)
    running --mark_cancelled-->   cancelled               (owning worker only)
    running --requeue/recover-->  queued, or cancelled if cancellation was requested

A running job belongs to the worker that claimed it. Updates from any other worker
(for example one that stalled and was recovered) change nothing and return False.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from iftyedits.core.models import CancelOutcome, Job, JobStatus, Stage
from iftyedits.storage.db import transaction, utc_now

IMPORT_FILE = "import_file"
PROCESS_VIDEO = "process_video"
TRANSCRIBE_VIDEO = "transcribe_video"
DOWNLOAD_MODEL = "download_model"
ANALYZE_CLIPS = "analyze_clips"
RENDER_SHORT = "render_short"
ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
CANCELLED_MESSAGE = "Cancelled by user"
_COLUMNS = (
    "id, kind, payload, status, progress, message, error, result, attempts, "
    "created_at, started_at, finished_at, heartbeat_at, worker_id, cancel_requested_at"
)


def _cutoff(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        status=JobStatus(row["status"]),
        progress=row["progress"],
        message=row["message"],
        error=row["error"],
        result=json.loads(row["result"]) if row["result"] else None,
        attempts=row["attempts"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        heartbeat_at=row["heartbeat_at"],
        worker_id=row["worker_id"],
        cancel_requested_at=row["cancel_requested_at"],
    )


def _owner_clause(worker_id: str | None) -> tuple[str, tuple[str, ...]]:
    """SQL restricting an update to running jobs (owned by ``worker_id`` when given)."""
    if worker_id is None:
        return "status = 'running'", ()
    return "status = 'running' AND worker_id = ?", (worker_id,)


class JobQueue:
    """Enqueue, claim, cancel and update jobs. Safe across processes via SQLite locking."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ producer

    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        """Add a job and return its id."""
        with transaction(self.conn):
            return self._insert(kind, payload)

    def enqueue_imports(self, paths: Iterable[Path], *, process_after: bool = False) -> list[int]:
        """Queue one import job per path, skipping paths already queued or running.

        With ``process_after`` the worker queues media preparation once the import succeeds.
        """
        ids: list[int] = []
        with transaction(self.conn):
            for path in paths:
                active = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE kind = ? AND status IN (?, ?) "
                    "AND json_extract(payload, '$.path') = ?",
                    (IMPORT_FILE, *ACTIVE_STATUSES, str(path)),
                ).fetchone()
                if active is None:
                    payload: dict[str, Any] = {"path": str(path)}
                    if process_after:
                        payload["process"] = True
                    ids.append(self._insert(IMPORT_FILE, payload))
        return ids

    def enqueue_processing(
        self, video_id: int, stages: Sequence[Stage], *, force: bool = False
    ) -> tuple[int, bool]:
        """Queue media preparation for a video.

        Returns ``(job_id, created)``. If the video already has a queued or running
        processing job, that job's id is returned and nothing is added.
        """
        if not stages:
            raise ValueError("At least one stage is required")
        with transaction(self.conn):
            existing = self._active_processing(video_id)
            if existing is not None:
                return existing, False
            payload = {
                "video_id": video_id,
                "stages": [Stage(s).value for s in stages],
                "force": force,
            }
            return self._insert(PROCESS_VIDEO, payload), True

    def enqueue_transcription(
        self,
        video_id: int,
        *,
        model: str,
        language: str | None,
        force: bool = False,
    ) -> tuple[int, bool]:
        """Queue transcription of a video (``language=None`` detects it).

        Returns ``(job_id, created)``. If the video already has a queued or running
        transcription job, that job's id is returned and nothing is added.
        """
        with transaction(self.conn):
            existing = self._active(TRANSCRIBE_VIDEO, "video_id", video_id)
            if existing is not None:
                return existing, False
            payload = {"video_id": video_id, "model": model, "language": language, "force": force}
            return self._insert(TRANSCRIBE_VIDEO, payload), True

    def enqueue_clip_analysis(self, video_id: int, *, force: bool = False) -> tuple[int, bool]:
        """Queue clip analysis of a video; reuses an active analysis job of the same video.

        Returns ``(job_id, created)``.
        """
        with transaction(self.conn):
            existing = self._active(ANALYZE_CLIPS, "video_id", video_id)
            if existing is not None:
                return existing, False
            return self._insert(ANALYZE_CLIPS, {"video_id": video_id, "force": force}), True

    def enqueue_short_render(self, render_id: int, video_id: int) -> tuple[int, bool]:
        """Queue a Short preview or export; reuses an active job for the same render row.

        Returns ``(job_id, created)``.
        """
        with transaction(self.conn):
            existing = self._active(RENDER_SHORT, "render_id", render_id)
            if existing is not None:
                return existing, False
            payload = {"render_id": render_id, "video_id": video_id}
            return self._insert(RENDER_SHORT, payload), True

    def enqueue_model_download(self, model: str) -> tuple[int, bool]:
        """Queue a speech-model download; reuses an active download of the same model."""
        with transaction(self.conn):
            existing = self._active(DOWNLOAD_MODEL, "model", model)
            if existing is not None:
                return existing, False
            return self._insert(DOWNLOAD_MODEL, {"model": model}), True

    def _active_processing(self, video_id: int) -> int | None:
        return self._active(PROCESS_VIDEO, "video_id", video_id)

    def _active(self, kind: str, key: str, value: int | str) -> int | None:
        if not key.isidentifier():
            raise ValueError(f"Invalid payload key {key!r}")
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND status IN (?, ?) "
            "AND json_extract(payload, ?) = ? ORDER BY id LIMIT 1",
            (kind, *ACTIVE_STATUSES, f"$.{key}", value),
        ).fetchone()
        return int(row["id"]) if row else None

    def _insert(self, kind: str, payload: dict[str, Any]) -> int:
        cursor = self.conn.execute(
            "INSERT INTO jobs (kind, payload, status, created_at) VALUES (?, ?, ?, ?)",
            (kind, json.dumps(payload), JobStatus.QUEUED.value, utc_now()),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Job insert returned no id")
        return cursor.lastrowid

    # ------------------------------------------------------------------ queries

    def get(self, job_id: int) -> Job | None:
        """Return one job."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE id = ?",  # noqa: S608 - constant columns
            (job_id,),
        ).fetchone()
        return _job(row) if row else None

    def list_recent(self, limit: int = 50) -> list[Job]:
        """Newest first."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs ORDER BY id DESC LIMIT ?",  # noqa: S608
            (limit,),
        ).fetchall()
        return [_job(r) for r in rows]

    def list_active(self) -> list[Job]:
        """Queued and running jobs, oldest first."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE status IN (?, ?) ORDER BY id",  # noqa: S608
            ACTIVE_STATUSES,
        ).fetchall()
        return [_job(r) for r in rows]

    def active_processing(self, video_id: int) -> Job | None:
        """The queued or running processing job for a video, if any."""
        job_id = self._active_processing(video_id)
        return self.get(job_id) if job_id is not None else None

    def active_transcription(self, video_id: int) -> Job | None:
        """The queued or running transcription job for a video, if any."""
        job_id = self._active(TRANSCRIBE_VIDEO, "video_id", video_id)
        return self.get(job_id) if job_id is not None else None

    def active_clip_analysis(self, video_id: int) -> Job | None:
        """The queued or running clip analysis job for a video, if any."""
        job_id = self._active(ANALYZE_CLIPS, "video_id", video_id)
        return self.get(job_id) if job_id is not None else None

    def active_download(self, model: str) -> Job | None:
        """The queued or running download of a model, if any."""
        job_id = self._active(DOWNLOAD_MODEL, "model", model)
        return self.get(job_id) if job_id is not None else None

    def active_downloads(self) -> list[Job]:
        """All queued or running model downloads."""
        return [j for j in self.list_active() if j.kind == DOWNLOAD_MODEL]

    def latest(self, kind: str, video_id: int) -> Job | None:
        """Most recent job of a kind for a video, whatever its state."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE kind = ? "  # noqa: S608 - constant columns
            "AND json_extract(payload, '$.video_id') = ? ORDER BY id DESC LIMIT 1",
            (kind, video_id),
        ).fetchone()
        return _job(row) if row else None

    def latest_by_video(self, kind: str) -> dict[int, Job]:
        """Most recent job of a kind for every video that has one."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE id IN ("  # noqa: S608 - constant columns
            "SELECT max(id) FROM jobs WHERE kind = ? "
            "GROUP BY json_extract(payload, '$.video_id'))",
            (kind,),
        ).fetchall()
        jobs = (_job(r) for r in rows)
        return {j.video_id: j for j in jobs if j.video_id is not None}

    def count_active(self) -> int:
        """Number of queued or running jobs."""
        row = self.conn.execute(
            "SELECT count(*) FROM jobs WHERE status IN (?, ?)", ACTIVE_STATUSES
        ).fetchone()
        return int(row[0])

    def cancel_requested(self, job_id: int) -> bool:
        """True if cancellation was requested for this job."""
        row = self.conn.execute(
            "SELECT cancel_requested_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested_at"])

    # ------------------------------------------------------------------ cancellation

    def request_cancel(self, job_id: int) -> CancelOutcome:
        """Cancel a queued job now, or ask the worker to stop a running one."""
        now = utc_now()
        with transaction(self.conn):
            row = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return CancelOutcome.NOT_FOUND
            status = JobStatus(row["status"])
            if status is JobStatus.QUEUED:
                self.conn.execute(
                    "UPDATE jobs SET status = ?, cancel_requested_at = ?, finished_at = ?, "
                    "error = ? WHERE id = ?",
                    (JobStatus.CANCELLED.value, now, now, CANCELLED_MESSAGE, job_id),
                )
                return CancelOutcome.CANCELLED
            if status is JobStatus.RUNNING:
                self.conn.execute(
                    "UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) "
                    "WHERE id = ?",
                    (now, job_id),
                )
                return CancelOutcome.REQUESTED
        return CancelOutcome.NOT_ACTIVE

    # ------------------------------------------------------------------ consumer

    def claim(self, worker_id: str) -> Job | None:
        """Atomically take the oldest queued job."""
        now = utc_now()
        with transaction(self.conn):
            row = self.conn.execute(
                f"UPDATE jobs SET status = ?, started_at = ?, heartbeat_at = ?, "  # noqa: S608
                "worker_id = ?, attempts = attempts + 1, error = NULL, progress = 0 "
                "WHERE id = (SELECT id FROM jobs WHERE status = ? ORDER BY id LIMIT 1) "
                f"RETURNING {_COLUMNS}",
                (JobStatus.RUNNING.value, now, now, worker_id, JobStatus.QUEUED.value),
            ).fetchone()
        return _job(row) if row else None

    def heartbeat(
        self,
        job_id: int,
        progress: float,
        message: str | None = None,
        *,
        worker_id: str | None = None,
    ) -> bool:
        """Record liveness and progress (0.0 to 1.0). False if the job is no longer owned."""
        clause, owner = _owner_clause(worker_id)
        cursor = self.conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, progress = ?, message = COALESCE(?, message) "  # noqa: S608
            f"WHERE id = ? AND {clause}",
            (utc_now(), max(0.0, min(1.0, progress)), message, job_id, *owner),
        )
        return cursor.rowcount == 1

    def succeed(
        self,
        job_id: int,
        result: dict[str, Any],
        message: str | None = None,
        *,
        worker_id: str | None = None,
    ) -> bool:
        """Mark a running job finished. False if it was not running (or not owned)."""
        return self._finish(
            job_id,
            JobStatus.SUCCEEDED,
            worker_id=worker_id,
            result=result,
            message=message,
            progress=1.0,
        )

    def fail(self, job_id: int, error: str, *, worker_id: str | None = None) -> bool:
        """Mark a running job failed permanently. False if it was not running (or not owned)."""
        return self._finish(job_id, JobStatus.FAILED, worker_id=worker_id, error=error)

    def mark_cancelled(
        self, job_id: int, *, worker_id: str | None = None, result: dict[str, Any] | None = None
    ) -> bool:
        """Record that a running job stopped because of a cancellation request."""
        return self._finish(
            job_id,
            JobStatus.CANCELLED,
            worker_id=worker_id,
            error=CANCELLED_MESSAGE,
            result=result,
        )

    def requeue(self, job_id: int, *, worker_id: str | None = None) -> bool:
        """Return a running job to the queue (worker shutdown).

        A job whose cancellation was requested is cancelled instead.
        """
        clause, owner = _owner_clause(worker_id)
        now = utc_now()
        with transaction(self.conn):
            cancelled = self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ? "  # noqa: S608
                f"WHERE id = ? AND {clause} AND cancel_requested_at IS NOT NULL",
                (JobStatus.CANCELLED.value, now, CANCELLED_MESSAGE, job_id, *owner),
            ).rowcount
            requeued = self.conn.execute(
                "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL, progress = 0 "  # noqa: S608
                f"WHERE id = ? AND {clause}",
                (JobStatus.QUEUED.value, job_id, *owner),
            ).rowcount
        return bool(cancelled or requeued)

    def recover_stale(self, stale_after_s: float, max_attempts: int) -> int:
        """Handle running jobs whose worker went silent.

        Jobs with a pending cancellation are cancelled; jobs that already used
        ``max_attempts`` fail; the rest are requeued. Clearing ``worker_id`` means
        the silent worker can no longer update them.
        """
        cutoff = _cutoff(stale_after_s)
        now = utc_now()
        running = JobStatus.RUNNING.value
        with transaction(self.conn):
            cancelled = self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ?, worker_id = NULL "
                "WHERE status = ? AND heartbeat_at < ? AND cancel_requested_at IS NOT NULL",
                (JobStatus.CANCELLED.value, now, CANCELLED_MESSAGE, running, cutoff),
            ).rowcount
            failed = self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, worker_id = NULL, "
                "error = 'Worker stopped responding too many times' "
                "WHERE status = ? AND heartbeat_at < ? AND attempts >= ?",
                (JobStatus.FAILED.value, now, running, cutoff, max_attempts),
            ).rowcount
            requeued = self.conn.execute(
                "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL, progress = 0 "
                "WHERE status = ? AND heartbeat_at < ?",
                (JobStatus.QUEUED.value, running, cutoff),
            ).rowcount
        return int(cancelled) + int(failed) + int(requeued)

    def _finish(
        self,
        job_id: int,
        status: JobStatus,
        *,
        worker_id: str | None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        message: str | None = None,
        progress: float | None = None,
    ) -> bool:
        clause, owner = _owner_clause(worker_id)
        now = utc_now()
        cursor = self.conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, heartbeat_at = ?, result = ?, "  # noqa: S608
            "error = ?, message = COALESCE(?, message), progress = COALESCE(?, progress) "
            f"WHERE id = ? AND {clause}",
            (
                status.value,
                now,
                now,
                json.dumps(result) if result is not None else None,
                error,
                message,
                progress,
                job_id,
                *owner,
            ),
        )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------ workers

    def register_worker(self, worker_id: str, pid: int) -> None:
        """Announce a worker process."""
        now = utc_now()
        self.conn.execute(
            "INSERT INTO workers (worker_id, pid, started_at, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (worker_id) DO UPDATE SET last_seen = excluded.last_seen",
            (worker_id, pid, now, now),
        )

    def touch_worker(self, worker_id: str) -> None:
        """Update a worker's liveness timestamp."""
        self.conn.execute(
            "UPDATE workers SET last_seen = ? WHERE worker_id = ?", (utc_now(), worker_id)
        )

    def remove_worker(self, worker_id: str) -> None:
        """Remove a worker that shut down cleanly."""
        self.conn.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,))

    def live_workers(self, within_s: float = 15.0) -> list[str]:
        """Workers seen within the last ``within_s`` seconds."""
        rows = self.conn.execute(
            "SELECT worker_id FROM workers WHERE last_seen >= ? ORDER BY worker_id",
            (_cutoff(within_s),),
        ).fetchall()
        return [r[0] for r in rows]
