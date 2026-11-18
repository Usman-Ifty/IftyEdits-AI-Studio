"""SQLite-backed job queue shared by the UI (enqueue) and the worker (execute)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from iftyedits.core.models import Job, JobStatus
from iftyedits.storage.db import transaction, utc_now

IMPORT_FILE = "import_file"
ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
_COLUMNS = (
    "id, kind, payload, status, progress, message, error, result, attempts, "
    "created_at, started_at, finished_at, heartbeat_at, worker_id"
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
    )


class JobQueue:
    """Enqueue, claim and update jobs. Safe across processes via SQLite locking."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ producer

    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        """Add a job and return its id."""
        with transaction(self.conn):
            return self._insert(kind, payload)

    def enqueue_imports(self, paths: Iterable[Path]) -> list[int]:
        """Queue one import job per path, skipping paths already queued or running."""
        ids: list[int] = []
        with transaction(self.conn):
            for path in paths:
                active = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE kind = ? AND status IN (?, ?) "
                    "AND json_extract(payload, '$.path') = ?",
                    (IMPORT_FILE, *ACTIVE_STATUSES, str(path)),
                ).fetchone()
                if active is None:
                    ids.append(self._insert(IMPORT_FILE, {"path": str(path)}))
        return ids

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

    def count_active(self) -> int:
        """Number of queued or running jobs."""
        row = self.conn.execute(
            "SELECT count(*) FROM jobs WHERE status IN (?, ?)", ACTIVE_STATUSES
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ consumer

    def claim(self, worker_id: str) -> Job | None:
        """Atomically take the oldest queued job."""
        now = utc_now()
        with transaction(self.conn):
            row = self.conn.execute(
                f"UPDATE jobs SET status = ?, started_at = ?, heartbeat_at = ?, "  # noqa: S608
                "worker_id = ?, attempts = attempts + 1, error = NULL "
                "WHERE id = (SELECT id FROM jobs WHERE status = ? ORDER BY id LIMIT 1) "
                f"RETURNING {_COLUMNS}",
                (JobStatus.RUNNING.value, now, now, worker_id, JobStatus.QUEUED.value),
            ).fetchone()
        return _job(row) if row else None

    def heartbeat(self, job_id: int, progress: float, message: str | None = None) -> None:
        """Record liveness and progress (0.0 to 1.0) of a running job."""
        self.conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, progress = ?, message = COALESCE(?, message) "
            "WHERE id = ? AND status = ?",
            (utc_now(), max(0.0, min(1.0, progress)), message, job_id, JobStatus.RUNNING.value),
        )

    def succeed(self, job_id: int, result: dict[str, Any], message: str | None = None) -> None:
        """Mark a job finished."""
        self._finish(job_id, JobStatus.SUCCEEDED, result=result, message=message, progress=1.0)

    def fail(self, job_id: int, error: str) -> None:
        """Mark a job failed permanently."""
        self._finish(job_id, JobStatus.FAILED, error=error)

    def requeue(self, job_id: int) -> None:
        """Return a running job to the queue (worker shutdown)."""
        self.conn.execute(
            "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL, progress = 0 "
            "WHERE id = ? AND status = ?",
            (JobStatus.QUEUED.value, job_id, JobStatus.RUNNING.value),
        )

    def recover_stale(self, stale_after_s: float, max_attempts: int) -> int:
        """Requeue (or fail, after ``max_attempts``) running jobs whose worker went silent."""
        cutoff = _cutoff(stale_after_s)
        with transaction(self.conn):
            failed = self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, "
                "error = 'Worker stopped responding too many times' "
                "WHERE status = ? AND heartbeat_at < ? AND attempts >= ?",
                (JobStatus.FAILED.value, utc_now(), JobStatus.RUNNING.value, cutoff, max_attempts),
            ).rowcount
            requeued = self.conn.execute(
                "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL, progress = 0 "
                "WHERE status = ? AND heartbeat_at < ?",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value, cutoff),
            ).rowcount
        return int(failed) + int(requeued)

    def _finish(
        self,
        job_id: int,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        message: str | None = None,
        progress: float | None = None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, heartbeat_at = ?, result = ?, "
            "error = ?, message = COALESCE(?, message), progress = COALESCE(?, progress) "
            "WHERE id = ?",
            (
                status.value,
                now,
                now,
                json.dumps(result) if result is not None else None,
                error,
                message,
                progress,
                job_id,
            ),
        )

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
