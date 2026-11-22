"""Persistence for YouTube uploads. State changes are enforced in SQL."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from iftyedits.core import youtube as yt
from iftyedits.storage.db import transaction, utc_now

_COLUMNS = (
    "id, project_id, render_id, status, details, fingerprint, approved_at, started_at, "
    "finished_at, bytes_sent, youtube_video_id, error_kind, error, job_id, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class UploadRecord:
    """One upload row."""

    id: int
    project_id: int
    render_id: int
    status: str
    details: dict[str, Any]
    fingerprint: str
    approved_at: str | None
    started_at: str | None
    finished_at: str | None
    bytes_sent: int
    youtube_video_id: str | None
    error_kind: str | None
    error: str | None
    job_id: int | None
    created_at: str
    updated_at: str

    @property
    def label(self) -> str:
        """Plain-language status."""
        return yt.STATUS_LABELS.get(self.status, self.status)

    @property
    def studio_url(self) -> str | None:
        """Where the owner can open the uploaded video in YouTube Studio."""
        if not self.youtube_video_id:
            return None
        return f"https://studio.youtube.com/video/{self.youtube_video_id}/edit"


def _record(row: sqlite3.Row) -> UploadRecord:
    return UploadRecord(
        id=row["id"],
        project_id=row["project_id"],
        render_id=row["render_id"],
        status=row["status"],
        details=json.loads(row["details"]),
        fingerprint=row["fingerprint"],
        approved_at=row["approved_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        bytes_sent=row["bytes_sent"],
        youtube_video_id=row["youtube_video_id"],
        error_kind=row["error_kind"],
        error=row["error"],
        job_id=row["job_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class YouTubeRepository:
    """Reads and writes ``youtube_uploads``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def create_draft(self, details: yt.UploadDetails) -> UploadRecord:
        """Save details awaiting approval. Older unapproved drafts for the Short are cancelled."""
        now = utc_now()
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE youtube_uploads SET status = 'cancelled', finished_at = ?, updated_at = ? "
                "WHERE project_id = ? AND status IN ('draft', 'approved')",
                (now, now, details.project_id),
            )
            cur = self.conn.execute(
                "INSERT INTO youtube_uploads (project_id, render_id, status, details, "
                "fingerprint, created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                (
                    details.project_id,
                    details.render_id,
                    json.dumps(details.to_dict(), ensure_ascii=False),
                    details.fingerprint(),
                    now,
                    now,
                ),
            )
        record = self.get(int(cur.lastrowid or 0))
        if record is None:
            raise sqlite3.DatabaseError("The upload draft could not be read back.")
        return record

    def get(self, upload_id: int) -> UploadRecord | None:
        """One upload by id."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM youtube_uploads WHERE id = ?",  # noqa: S608
            (upload_id,),
        ).fetchone()
        return _record(row) if row else None

    def list_for_project(self, project_id: int, limit: int = 20) -> list[UploadRecord]:
        """Newest first."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM youtube_uploads WHERE project_id = ? "  # noqa: S608
            "ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [_record(r) for r in rows]

    def latest(self, project_id: int) -> UploadRecord | None:
        """Newest upload for a Short."""
        found = self.list_for_project(project_id, 1)
        return found[0] if found else None

    def approve(self, upload_id: int, fingerprint: str, job_id: int | None = None) -> bool:
        """draft -> approved, only if the fingerprint is the one shown to the owner."""
        now = utc_now()
        cur = self.conn.execute(
            "UPDATE youtube_uploads SET status = 'approved', approved_at = ?, job_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'draft' AND fingerprint = ?",
            (now, job_id, now, upload_id, fingerprint),
        )
        return cur.rowcount == 1

    def set_job(self, upload_id: int, job_id: int) -> None:
        """Remember the background job."""
        self.conn.execute(
            "UPDATE youtube_uploads SET job_id = ?, updated_at = ? WHERE id = ?",
            (job_id, utc_now(), upload_id),
        )

    def start(self, upload_id: int) -> bool:
        """approved -> uploading. Anything else (including a second start) returns False."""
        now = utc_now()
        cur = self.conn.execute(
            "UPDATE youtube_uploads SET status = 'uploading', started_at = ?, bytes_sent = 0, "
            "error_kind = NULL, error = NULL, updated_at = ? WHERE id = ? AND status = 'approved'",
            (now, now, upload_id),
        )
        return cur.rowcount == 1

    def progress(self, upload_id: int, sent: int) -> None:
        """Bytes confirmed by YouTube so far."""
        self.conn.execute(
            "UPDATE youtube_uploads SET bytes_sent = ?, updated_at = ? "
            "WHERE id = ? AND status = 'uploading'",
            (sent, utc_now(), upload_id),
        )

    def succeed(self, upload_id: int, video_id: str) -> None:
        """uploading -> uploaded."""
        now = utc_now()
        self.conn.execute(
            "UPDATE youtube_uploads SET status = 'uploaded', youtube_video_id = ?, "
            "bytes_sent = COALESCE(json_extract(details, '$.file_size'), bytes_sent), "
            "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'uploading'",
            (video_id, now, now, upload_id),
        )

    def fail(self, upload_id: int, kind: str, message: str) -> None:
        """Any unfinished state -> failed, with a plain error."""
        now = utc_now()
        self.conn.execute(
            "UPDATE youtube_uploads SET status = 'failed', error_kind = ?, error = ?, "
            "finished_at = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('draft', 'approved', 'uploading')",
            (kind, message[:500], now, now, upload_id),
        )

    def reapprove_after_failure(self, upload_id: int) -> bool:
        """failed -> approved so the same approved details can be retried (same fingerprint)."""
        cur = self.conn.execute(
            "UPDATE youtube_uploads SET status = 'approved', error_kind = NULL, error = NULL, "
            "finished_at = NULL, updated_at = ? WHERE id = ? AND status = 'failed' "
            "AND approved_at IS NOT NULL AND youtube_video_id IS NULL",
            (utc_now(), upload_id),
        )
        return cur.rowcount == 1

    def cancel(self, upload_id: int) -> bool:
        """Cancel before the upload has started."""
        now = utc_now()
        cur = self.conn.execute(
            "UPDATE youtube_uploads SET status = 'cancelled', finished_at = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('draft', 'approved')",
            (now, now, upload_id),
        )
        return cur.rowcount == 1

    def started_since(self, since_iso: str) -> int:
        """Uploads that began at or after a time (used for the soft daily limit)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM youtube_uploads WHERE started_at >= ?", (since_iso,)
        ).fetchone()
        return int(row[0])
