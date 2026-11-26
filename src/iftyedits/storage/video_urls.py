"""Persistence for videos fetched from a URL (see migration 0007)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from iftyedits.core.sources import SourceMetadata
from iftyedits.storage.db import transaction, utc_now

_COLUMNS = (
    "id, source_key, url, video_id, title, uploader, duration_s, upload_date, license, "
    "status, error, fetched_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class VideoUrlRecord:
    """A pasted link and what became of it."""

    id: int
    source_key: str
    url: str
    video_id: int | None
    title: str
    uploader: str
    duration_s: float | None
    upload_date: str | None
    license: str | None
    status: str
    error: str | None
    fetched_at: str
    updated_at: str


def _record(row: sqlite3.Row) -> VideoUrlRecord:
    return VideoUrlRecord(**{key: row[key] for key in row.keys()})  # noqa: SIM118


class VideoUrlRepository:
    """Records which links were fetched and the originals they became."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, source_key: str) -> VideoUrlRecord | None:
        """The record for a source key, or None."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM video_urls WHERE source_key = ?",  # noqa: S608 - constant columns
            (source_key,),
        ).fetchone()
        return _record(row) if row else None

    def for_video(self, video_id: int) -> VideoUrlRecord | None:
        """The link a stored original came from, or None for local imports."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM video_urls WHERE video_id = ? ORDER BY id LIMIT 1",  # noqa: S608
            (video_id,),
        ).fetchone()
        return _record(row) if row else None

    def start(self, meta: SourceMetadata) -> VideoUrlRecord:
        """Create or refresh the pending record for a link (a downloaded one keeps its video)."""
        now = utc_now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO video_urls (source_key, url, title, uploader, duration_s, "
                "upload_date, license, status, fetched_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT (source_key) DO UPDATE SET title = excluded.title, "
                "uploader = excluded.uploader, duration_s = excluded.duration_s, "
                "upload_date = excluded.upload_date, license = excluded.license, "
                "status = CASE WHEN video_id IS NULL THEN 'pending' ELSE status END, "
                "error = NULL, updated_at = excluded.updated_at",
                (
                    meta.source_key,
                    meta.url,
                    meta.title,
                    meta.uploader,
                    meta.duration_s,
                    meta.upload_date,
                    meta.license,
                    now,
                    now,
                ),
            )
        found = self.get(meta.source_key)
        if found is None:
            raise sqlite3.DatabaseError(f"video_urls row missing for {meta.source_key}")
        return found

    def mark_downloaded(self, source_key: str, video_id: int) -> None:
        """Link the record to the stored original."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE video_urls SET video_id = ?, status = 'downloaded', error = NULL, "
                "updated_at = ? WHERE source_key = ?",
                (video_id, utc_now(), source_key),
            )

    def mark_failed(self, source_key: str, error: str) -> None:
        """Remember why a fetch failed (only while no original is linked)."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE video_urls SET status = 'failed', error = ?, updated_at = ? "
                "WHERE source_key = ? AND video_id IS NULL",
                (error[:500], utc_now(), source_key),
            )
