"""Persistence for processing stages and generated artifacts.

Every write made on behalf of a job first checks, inside the same transaction, that
the job is still running and owned by the calling worker. A worker that lost its
job (for example after stale recovery) therefore cannot overwrite newer results.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from iftyedits.core.errors import JobOwnershipError
from iftyedits.core.models import (
    Artifact,
    ArtifactKind,
    JobStatus,
    Stage,
    StageRecord,
    StageStatus,
)
from iftyedits.storage.db import transaction, utc_now

_STAGE_COLUMNS = (
    "id, video_id, stage, status, job_id, params_hash, params_json, progress, message, "
    "error, started_at, finished_at"
)
_ARTIFACT_COLUMNS = (
    "id, video_id, stage_id, job_id, kind, relpath, size_bytes, sha256, params_hash, "
    "metadata_json, created_at"
)


def _stage(row: sqlite3.Row) -> StageRecord:
    return StageRecord(
        id=row["id"],
        video_id=row["video_id"],
        stage=Stage(row["stage"]),
        status=StageStatus(row["status"]),
        job_id=row["job_id"],
        params_hash=row["params_hash"],
        params=json.loads(row["params_json"]),
        progress=row["progress"],
        message=row["message"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        video_id=row["video_id"],
        stage_id=row["stage_id"],
        job_id=row["job_id"],
        kind=ArtifactKind(row["kind"]),
        relpath=row["relpath"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        params_hash=row["params_hash"],
        metadata=json.loads(row["metadata_json"]),
        created_at=row["created_at"],
    )


def assert_job_owned(conn: sqlite3.Connection, job_id: int | None, worker_id: str | None) -> None:
    """Raise ``JobOwnershipError`` unless ``job_id`` is running (on ``worker_id`` if given).

    Call inside the transaction that performs the write. ``job_id=None`` (work done
    outside a job, for example from tests or the CLI) is always allowed.
    """
    if job_id is None:
        return
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE id = ? AND status = ? AND (? IS NULL OR worker_id = ?)",
        (job_id, JobStatus.RUNNING.value, worker_id, worker_id),
    ).fetchone()
    if row is None:
        raise JobOwnershipError(f"Job {job_id} is no longer running on this worker")


class ProcessingRepository:
    """Reads and writes ``processing_stages`` and ``artifacts``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ ownership

    def _assert_owned(self, job_id: int | None, worker_id: str | None) -> None:
        assert_job_owned(self.conn, job_id, worker_id)

    # ------------------------------------------------------------------ stages

    def start_stage(
        self,
        video_id: int,
        stage: Stage,
        *,
        params_hash: str,
        params: dict[str, Any],
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> StageRecord:
        """Mark a stage running for a video (one row per video and stage, reset each run)."""
        now = utc_now()
        with transaction(self.conn):
            self._assert_owned(job_id, worker_id)
            self.conn.execute(
                "INSERT INTO processing_stages (video_id, stage, status, job_id, params_hash, "
                "params_json, progress, message, error, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, NULL) "
                "ON CONFLICT (video_id, stage) DO UPDATE SET status = excluded.status, "
                "job_id = excluded.job_id, params_hash = excluded.params_hash, "
                "params_json = excluded.params_json, progress = 0, message = NULL, "
                "error = NULL, started_at = excluded.started_at, finished_at = NULL",
                (
                    video_id,
                    stage.value,
                    StageStatus.RUNNING.value,
                    job_id,
                    params_hash,
                    json.dumps(params, sort_keys=True),
                    now,
                ),
            )
            row = self.conn.execute(
                f"SELECT {_STAGE_COLUMNS} FROM processing_stages "  # noqa: S608
                "WHERE video_id = ? AND stage = ?",
                (video_id, stage.value),
            ).fetchone()
        return _stage(row)

    def update_progress(self, stage_id: int, progress: float, message: str | None = None) -> None:
        """Record progress of a running stage (0.0 to 1.0)."""
        self.conn.execute(
            "UPDATE processing_stages SET progress = ?, message = COALESCE(?, message) "
            "WHERE id = ? AND status = ?",
            (max(0.0, min(1.0, progress)), message, stage_id, StageStatus.RUNNING.value),
        )

    def finish_stage(
        self,
        stage_id: int,
        status: StageStatus,
        *,
        message: str | None = None,
        error: str | None = None,
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Record the final state of a running stage."""
        if status is StageStatus.RUNNING:
            raise ValueError("finish_stage needs a final status")
        with transaction(self.conn):
            self._assert_owned(job_id, worker_id)
            self.conn.execute(
                "UPDATE processing_stages SET status = ?, finished_at = ?, "
                "message = COALESCE(?, message), error = ?, "
                "progress = CASE WHEN ? IN ('succeeded', 'skipped') THEN 1 ELSE progress END "
                "WHERE id = ?",
                (status.value, utc_now(), message, error, status.value, stage_id),
            )

    def record_skipped(
        self,
        video_id: int,
        stage: Stage,
        *,
        params_hash: str,
        params: dict[str, Any],
        message: str,
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> StageRecord:
        """Record a stage that does not apply to this video (for example no audio)."""
        record = self.start_stage(
            video_id,
            stage,
            params_hash=params_hash,
            params=params,
            job_id=job_id,
            worker_id=worker_id,
        )
        self.finish_stage(
            record.id, StageStatus.SKIPPED, message=message, job_id=job_id, worker_id=worker_id
        )
        stage_record = self.stage(video_id, stage)
        if stage_record is None:
            raise RuntimeError("Stage record vanished")
        return stage_record

    def stage(self, video_id: int, stage: Stage) -> StageRecord | None:
        """Latest record of one stage."""
        row = self.conn.execute(
            f"SELECT {_STAGE_COLUMNS} FROM processing_stages "  # noqa: S608
            "WHERE video_id = ? AND stage = ?",
            (video_id, stage.value),
        ).fetchone()
        return _stage(row) if row else None

    def stages(self, video_id: int) -> list[StageRecord]:
        """All stage records of a video, in pipeline order."""
        rows = self.conn.execute(
            f"SELECT {_STAGE_COLUMNS} FROM processing_stages WHERE video_id = ?",  # noqa: S608
            (video_id,),
        ).fetchall()
        order = {s: i for i, s in enumerate(Stage)}
        return sorted((_stage(r) for r in rows), key=lambda s: order[s.stage])

    def stage_summary(self) -> dict[int, dict[Stage, StageStatus]]:
        """Stage status per video for every video that has any stage record."""
        summary: dict[int, dict[Stage, StageStatus]] = {}
        for row in self.conn.execute("SELECT video_id, stage, status FROM processing_stages"):
            summary.setdefault(row["video_id"], {})[Stage(row["stage"])] = StageStatus(
                row["status"]
            )
        return summary

    def fail_orphaned_running(self) -> int:
        """Mark stages left ``running`` by a job that is no longer running as failed."""
        cursor = self.conn.execute(
            "UPDATE processing_stages SET status = ?, finished_at = ?, "
            "error = 'Interrupted: the job stopped before this stage finished' "
            "WHERE status = ? AND (job_id IS NULL OR job_id NOT IN "
            "(SELECT id FROM jobs WHERE status = ?))",
            (
                StageStatus.FAILED.value,
                utc_now(),
                StageStatus.RUNNING.value,
                JobStatus.RUNNING.value,
            ),
        )
        return int(cursor.rowcount)

    # ------------------------------------------------------------------ artifacts

    def register_artifact(
        self,
        *,
        video_id: int,
        stage_id: int,
        kind: ArtifactKind,
        relpath: str,
        size_bytes: int,
        sha256: str,
        params_hash: str,
        metadata: dict[str, Any],
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> tuple[Artifact, str | None]:
        """Record the current artifact of a kind for a video.

        Returns ``(artifact, replaced_relpath)``; ``replaced_relpath`` is the previous
        file of this kind when it lived at a different path, so the caller can remove it.
        """
        with transaction(self.conn):
            self._assert_owned(job_id, worker_id)
            previous = self.conn.execute(
                "SELECT relpath FROM artifacts WHERE video_id = ? AND kind = ?",
                (video_id, kind.value),
            ).fetchone()
            self.conn.execute(
                "INSERT INTO artifacts (video_id, stage_id, job_id, kind, relpath, size_bytes, "
                "sha256, params_hash, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (video_id, kind) DO UPDATE SET stage_id = excluded.stage_id, "
                "job_id = excluded.job_id, relpath = excluded.relpath, "
                "size_bytes = excluded.size_bytes, sha256 = excluded.sha256, "
                "params_hash = excluded.params_hash, metadata_json = excluded.metadata_json, "
                "created_at = excluded.created_at",
                (
                    video_id,
                    stage_id,
                    job_id,
                    kind.value,
                    relpath,
                    size_bytes,
                    sha256,
                    params_hash,
                    json.dumps(metadata, sort_keys=True),
                    utc_now(),
                ),
            )
        artifact = self.artifact(video_id, kind)
        if artifact is None:
            raise RuntimeError("Artifact vanished after insert")
        replaced = previous["relpath"] if previous and previous["relpath"] != relpath else None
        return artifact, replaced

    def artifact(self, video_id: int, kind: ArtifactKind) -> Artifact | None:
        """Current artifact of a kind for a video."""
        row = self.conn.execute(
            f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE video_id = ? AND kind = ?",  # noqa: S608
            (video_id, kind.value),
        ).fetchone()
        return _artifact(row) if row else None

    def artifacts(self, video_id: int) -> list[Artifact]:
        """All artifacts of a video."""
        rows = self.conn.execute(
            f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE video_id = ? ORDER BY kind",  # noqa: S608
            (video_id,),
        ).fetchall()
        return [_artifact(r) for r in rows]

    def relpath_in_use(self, relpath: str) -> bool:
        """True if any artifact row points at this path."""
        row = self.conn.execute("SELECT 1 FROM artifacts WHERE relpath = ?", (relpath,)).fetchone()
        return row is not None
