"""Persistence for Short projects (editable settings per suggested clip) and their renders."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from iftyedits.core.errors import ShortConfigError
from iftyedits.core.romanize import romanize
from iftyedits.core.shorts import ShortConfig
from iftyedits.storage.db import transaction, utc_now

KIND_PREVIEW = "preview"
KIND_EXPORT = "export"
ACTIVE = ("queued", "running")


@dataclass(frozen=True, slots=True)
class ShortProject:
    """An editable Short for one suggested clip."""

    id: int
    video_id: int
    candidate_key: str
    title: str
    original_start_s: float
    original_end_s: float
    config: ShortConfig
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ShortRender:
    """One preview or export attempt."""

    id: int
    project_id: int
    kind: str
    config_hash: str
    config: ShortConfig
    status: str
    job_id: int | None
    output_path: Path | None
    width: int | None
    height: int | None
    duration_s: float | None
    size_bytes: int | None
    has_audio: bool | None
    encoder: str | None
    error: str | None
    created_at: str
    finished_at: str | None

    @property
    def active(self) -> bool:
        """Queued or running."""
        return self.status in ACTIVE


_PROJECT_COLUMNS = (
    "id, video_id, candidate_key, title, original_start_s, original_end_s, config_json, "
    "created_at, updated_at"
)
_RENDER_COLUMNS = (
    "id, project_id, kind, config_hash, config_json, status, job_id, output_path, width, "
    "height, duration_s, size_bytes, has_audio, encoder, error, created_at, finished_at"
)


def _project(row: sqlite3.Row) -> ShortProject:
    return ShortProject(
        id=row["id"],
        video_id=row["video_id"],
        candidate_key=row["candidate_key"],
        title=romanize(row["title"]),
        original_start_s=row["original_start_s"],
        original_end_s=row["original_end_s"],
        config=ShortConfig.from_json(row["config_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _render(row: sqlite3.Row) -> ShortRender:
    return ShortRender(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        config_hash=row["config_hash"],
        config=ShortConfig.from_json(row["config_json"]),
        status=row["status"],
        job_id=row["job_id"],
        output_path=Path(row["output_path"]) if row["output_path"] else None,
        width=row["width"],
        height=row["height"],
        duration_s=row["duration_s"],
        size_bytes=row["size_bytes"],
        has_audio=None if row["has_audio"] is None else bool(row["has_audio"]),
        encoder=row["encoder"],
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


class ShortRepository:
    """Projects and renders. Never touches clip suggestions or media files."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ projects

    def get_project(self, project_id: int) -> ShortProject | None:
        """One project by id."""
        row = self.conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM short_projects WHERE id = ?",  # noqa: S608
            (project_id,),
        ).fetchone()
        return _project(row) if row else None

    def find_project(self, video_id: int, candidate_key: str) -> ShortProject | None:
        """The project for a suggestion, if one was started."""
        row = self.conn.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM short_projects "  # noqa: S608
            "WHERE video_id = ? AND candidate_key = ?",
            (video_id, candidate_key),
        ).fetchone()
        return _project(row) if row else None

    def list_projects(self, video_id: int | None = None) -> list[ShortProject]:
        """Newest edit first."""
        sql = f"SELECT {_PROJECT_COLUMNS} FROM short_projects"  # noqa: S608
        args: tuple[int, ...] = ()
        if video_id is not None:
            sql += " WHERE video_id = ?"
            args = (video_id,)
        rows = self.conn.execute(sql + " ORDER BY updated_at DESC, id DESC", args).fetchall()
        return [_project(r) for r in rows]

    def create_project(
        self,
        video_id: int,
        candidate_key: str,
        title: str,
        original_start_s: float,
        original_end_s: float,
        config: ShortConfig,
    ) -> ShortProject:
        """Create the project, or return the existing one for this suggestion."""
        now = utc_now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO short_projects (video_id, candidate_key, title, "
                "original_start_s, original_end_s, config_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    video_id,
                    candidate_key,
                    title,
                    original_start_s,
                    original_end_s,
                    config.to_json(),
                    now,
                    now,
                ),
            )
        project = self.find_project(video_id, candidate_key)
        if project is None:  # pragma: no cover - the insert above guarantees a row
            raise ShortConfigError("Could not create the Short project")
        return project

    def save_config(self, project_id: int, config: ShortConfig) -> None:
        """Store new settings. The original suggestion boundaries are not touched."""
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE short_projects SET config_json = ?, updated_at = ? WHERE id = ?",
                (config.to_json(), utc_now(), project_id),
            )
        if cursor.rowcount == 0:
            raise ShortConfigError("That Short project no longer exists")

    # ------------------------------------------------------------------ renders

    def get_render(self, render_id: int) -> ShortRender | None:
        """One render by id."""
        row = self.conn.execute(
            f"SELECT {_RENDER_COLUMNS} FROM short_renders WHERE id = ?",  # noqa: S608
            (render_id,),
        ).fetchone()
        return _render(row) if row else None

    def list_renders(self, project_id: int, kind: str | None = None) -> list[ShortRender]:
        """Newest first."""
        sql = f"SELECT {_RENDER_COLUMNS} FROM short_renders WHERE project_id = ?"  # noqa: S608
        args: tuple[int | str, ...] = (project_id,)
        if kind is not None:
            sql += " AND kind = ?"
            args = (project_id, kind)
        rows = self.conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [_render(r) for r in rows]

    def find_active(self, project_id: int, kind: str, config_hash: str) -> ShortRender | None:
        """A queued or running render of the identical request."""
        row = self.conn.execute(
            f"SELECT {_RENDER_COLUMNS} FROM short_renders "  # noqa: S608
            "WHERE project_id = ? AND kind = ? AND config_hash = ? "
            "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
            (project_id, kind, config_hash),
        ).fetchone()
        return _render(row) if row else None

    def find_succeeded(self, project_id: int, kind: str, config_hash: str) -> ShortRender | None:
        """The newest successful render of the identical request whose file still exists."""
        rows = self.conn.execute(
            f"SELECT {_RENDER_COLUMNS} FROM short_renders "  # noqa: S608
            "WHERE project_id = ? AND kind = ? AND config_hash = ? AND status = 'succeeded' "
            "ORDER BY id DESC",
            (project_id, kind, config_hash),
        ).fetchall()
        for row in rows:
            render = _render(row)
            if render.output_path is not None and render.output_path.is_file():
                return render
        return None

    def create_render(
        self, project_id: int, kind: str, config_hash: str, config: ShortConfig
    ) -> ShortRender:
        """Add a queued render. Raises ``sqlite3.IntegrityError`` if an identical one is active."""
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO short_renders (project_id, kind, config_hash, config_json, status, "
                "created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                (project_id, kind, config_hash, config.to_json(), utc_now()),
            )
        render = self.get_render(cursor.lastrowid or 0)
        if render is None:  # pragma: no cover
            raise ShortConfigError("Could not create the render record")
        return render

    def attach_job(self, render_id: int, job_id: int) -> None:
        """Link the queue job that will do the work."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE short_renders SET job_id = ? WHERE id = ?", (job_id, render_id)
            )

    def mark_running(self, render_id: int) -> None:
        """The worker started."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE short_renders SET status = 'running', error = NULL "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (render_id,),
            )

    def mark_succeeded(
        self,
        render_id: int,
        *,
        output_path: Path,
        width: int,
        height: int,
        duration_s: float,
        size_bytes: int,
        has_audio: bool,
        encoder: str,
    ) -> None:
        """Record a verified output."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE short_renders SET status = 'succeeded', output_path = ?, width = ?, "
                "height = ?, duration_s = ?, size_bytes = ?, has_audio = ?, encoder = ?, "
                "error = NULL, finished_at = ? WHERE id = ?",
                (
                    str(output_path),
                    width,
                    height,
                    duration_s,
                    size_bytes,
                    int(has_audio),
                    encoder,
                    utc_now(),
                    render_id,
                ),
            )

    def mark_failed(self, render_id: int, error: str, *, cancelled: bool = False) -> None:
        """Record a failed or cancelled render. A finished render is left alone."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE short_renders SET status = ?, error = ?, finished_at = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                ("cancelled" if cancelled else "failed", error[:2000], utc_now(), render_id),
            )

    def reconcile(self, grace_s: float = 60.0) -> int:
        """Fail renders left queued or running whose job is gone or finished.

        Rows younger than ``grace_s`` are left alone (their job may not be attached yet).
        Returns how many rows changed. Run when the worker starts, so a crashed worker does
        not leave an export looking busy forever.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=grace_s)).isoformat(timespec="microseconds")
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE short_renders SET status = 'failed', finished_at = ?, "
                "error = 'The export was interrupted before it finished' "
                "WHERE status IN ('queued', 'running') AND created_at < ? "
                "AND (job_id IS NULL OR job_id NOT IN "
                "(SELECT id FROM jobs WHERE status IN ('queued', 'running')))",
                (utc_now(), cutoff),
            )
        return cursor.rowcount
