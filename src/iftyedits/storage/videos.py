"""Video repository: persistence for imported originals, sources and streams."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from iftyedits.core.errors import InvalidSourceError
from iftyedits.core.models import MediaInfo, StreamInfo, Video, VideoSource, VideoStatus
from iftyedits.storage.db import transaction, utc_now

_VIDEO_COLUMNS = (
    "id, sha256, vault_relpath, original_filename, size_bytes, status, format_name, "
    "duration_s, bit_rate, probe_error, imported_at"
)
_STREAM_FIELDS = (
    "codec_type",
    "codec_name",
    "profile",
    "width",
    "height",
    "pix_fmt",
    "avg_frame_rate",
    "r_frame_rate",
    "time_base",
    "rotation",
    "color_transfer",
    "color_primaries",
    "sample_rate",
    "channels",
    "channel_layout",
    "language",
    "bit_rate",
    "duration_s",
)


def _video(row: sqlite3.Row) -> Video:
    return Video(
        id=row["id"],
        sha256=row["sha256"],
        vault_relpath=row["vault_relpath"],
        original_filename=row["original_filename"],
        size_bytes=row["size_bytes"],
        status=VideoStatus(row["status"]),
        format_name=row["format_name"],
        duration_s=row["duration_s"],
        bit_rate=row["bit_rate"],
        probe_error=row["probe_error"],
        imported_at=row["imported_at"],
    )


class VideoRepository:
    """Reads and writes ``videos``, ``video_sources`` and ``media_streams``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, video_id: int) -> Video | None:
        """Return a video by id."""
        row = self.conn.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM videos WHERE id = ?",  # noqa: S608 - constant columns
            (video_id,),
        ).fetchone()
        return _video(row) if row else None

    def get_by_sha(self, sha256: str) -> Video | None:
        """Return a video by content hash."""
        row = self.conn.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM videos WHERE sha256 = ?",  # noqa: S608
            (sha256,),
        ).fetchone()
        return _video(row) if row else None

    def find_by_size(self, size_bytes: int) -> list[Video]:
        """Return videos with exactly this size (duplicate candidates)."""
        rows = self.conn.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM videos WHERE size_bytes = ?",  # noqa: S608
            (size_bytes,),
        ).fetchall()
        return [_video(r) for r in rows]

    def list_recent(self, limit: int = 500) -> list[Video]:
        """Most recently imported first."""
        rows = self.conn.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM videos ORDER BY imported_at DESC, id DESC LIMIT ?",  # noqa: S608
            (limit,),
        ).fetchall()
        return [_video(r) for r in rows]

    def resolve_reference(self, reference: str) -> Video:
        """Find a video by numeric id or by a unique SHA-256 prefix (6+ hex characters)."""
        ref = reference.strip().lower()
        if ref.isdigit():
            video = self.get(int(ref))
            if video is None:
                raise InvalidSourceError(f"No video with id {ref}")
            return video
        if len(ref) < 6 or any(c not in "0123456789abcdef" for c in ref):
            raise InvalidSourceError("Use a video id or a SHA-256 prefix of at least 6 characters")
        rows = self.conn.execute(
            f"SELECT {_VIDEO_COLUMNS} FROM videos WHERE substr(sha256, 1, ?) = ? LIMIT 2",  # noqa: S608
            (len(ref), ref),
        ).fetchall()
        if not rows:
            raise InvalidSourceError(f"No video matches {ref}")
        if len(rows) > 1:
            raise InvalidSourceError(f"Prefix {ref} is ambiguous; use more characters")
        return _video(rows[0])

    def known_hashes(self) -> set[str]:
        """All stored content hashes."""
        return {r[0] for r in self.conn.execute("SELECT sha256 FROM videos")}

    def sources(self, video_id: int) -> list[VideoSource]:
        """Every path this content was imported from."""
        rows = self.conn.execute(
            "SELECT video_id, source_path, imported_at FROM video_sources "
            "WHERE video_id = ? ORDER BY id",
            (video_id,),
        ).fetchall()
        return [VideoSource(r["video_id"], r["source_path"], r["imported_at"]) for r in rows]

    def streams(self, video_id: int) -> list[StreamInfo]:
        """Probed streams in index order."""
        rows = self.conn.execute(
            f"SELECT stream_index, is_attached_pic, {', '.join(_STREAM_FIELDS)} "  # noqa: S608
            "FROM media_streams WHERE video_id = ? ORDER BY stream_index",
            (video_id,),
        ).fetchall()
        return [
            StreamInfo(
                index=r["stream_index"],
                is_attached_pic=bool(r["is_attached_pic"]),
                **{name: r[name] for name in _STREAM_FIELDS},
            )
            for r in rows
        ]

    def add_source(self, video_id: int, source_path: Path) -> None:
        """Record another location this content was imported from."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO video_sources (video_id, source_path, imported_at) "
                "VALUES (?, ?, ?) ON CONFLICT (video_id, source_path) DO NOTHING",
                (video_id, str(source_path), utc_now()),
            )

    def record_import(
        self,
        *,
        sha256: str,
        vault_relpath: str,
        original_filename: str,
        size_bytes: int,
        info: MediaInfo | None,
        probe_error: str | None,
        source_path: Path | None,
    ) -> tuple[Video, bool]:
        """Insert a video (if new), its streams and its source, atomically.

        Returns ``(video, created)``. Re-recording existing content only adds the source.
        """
        now = utc_now()
        status = VideoStatus.READY if info is not None else VideoStatus.PROBE_FAILED
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO videos (sha256, vault_relpath, original_filename, size_bytes, "
                "status, format_name, duration_s, bit_rate, probe_json, probe_error, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (sha256) DO NOTHING",
                (
                    sha256,
                    vault_relpath,
                    original_filename,
                    size_bytes,
                    status.value,
                    info.format_name if info else None,
                    info.duration_s if info else None,
                    info.bit_rate if info else None,
                    info.raw_json if info else None,
                    probe_error,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = self.conn.execute("SELECT id FROM videos WHERE sha256 = ?", (sha256,)).fetchone()
            video_id = int(row["id"])
            if created and info is not None:
                self._insert_streams(video_id, info.streams)
            if source_path is not None:
                self.conn.execute(
                    "INSERT INTO video_sources (video_id, source_path, imported_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (video_id, source_path) DO NOTHING",
                    (video_id, str(source_path), now),
                )
        video = self.get(video_id)
        if video is None:
            raise RuntimeError(f"Video {sha256} vanished after insert")
        return video, created

    def _insert_streams(self, video_id: int, streams: tuple[StreamInfo, ...]) -> None:
        columns = ", ".join(("video_id", "stream_index", "is_attached_pic", *_STREAM_FIELDS))
        placeholders = ", ".join("?" * (3 + len(_STREAM_FIELDS)))
        self.conn.executemany(
            f"INSERT INTO media_streams ({columns}) VALUES ({placeholders})",  # noqa: S608
            [
                (
                    video_id,
                    s.index,
                    int(s.is_attached_pic),
                    *(getattr(s, name) for name in _STREAM_FIELDS),
                )
                for s in streams
            ],
        )
