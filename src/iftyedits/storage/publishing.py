"""Persistence for upload preparation: metadata, content reviews, rights and readiness."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields
from typing import Any

from iftyedits.core.metadata import Metadata
from iftyedits.core.rights import HISTORY_FIELDS, Rights
from iftyedits.core.safety import SafetyFlag
from iftyedits.storage.db import transaction, utc_now


@dataclass(frozen=True, slots=True)
class StoredMetadata:
    """Saved metadata and where each field came from."""

    project_id: int
    metadata: Metadata
    provenance: dict[str, str]
    model: str
    context_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredReview:
    """One content review run."""

    id: int
    project_id: int
    status: str
    risk_level: str
    model: str
    context_hash: str
    flags: tuple[SafetyFlag, ...]
    summary: str
    notes: tuple[str, ...]
    note: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """The owner's current answer to a flag."""

    flag_key: str
    action: str
    note: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredReadiness:
    """Saved checklist ticks, upload choices and the final confirmation."""

    project_id: int
    checks: frozenset[str]
    settings: dict[str, str]
    rights_ack: bool
    confirmed_at: str | None
    confirmed_render_id: int | None
    confirmed_fingerprint: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RightsChange:
    """One entry of the rights audit trail."""

    id: int
    old_status: str | None
    new_status: str
    changes: dict[str, list[str]]
    created_at: str


class PublishRepository:
    """Reads and writes upload-preparation data. Never touches media or clip settings."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ metadata

    def get_metadata(self, project_id: int) -> StoredMetadata | None:
        """Saved metadata for a Short, or None."""
        row = self.conn.execute(
            "SELECT * FROM publish_metadata WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return StoredMetadata(
            project_id=row["project_id"],
            metadata=Metadata.from_dict(_load(row["doc_json"])),
            provenance={str(k): str(v) for k, v in _load(row["provenance_json"]).items()},
            model=row["model"],
            context_hash=row["context_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_metadata(
        self,
        project_id: int,
        metadata: Metadata,
        provenance: dict[str, str],
        *,
        model: str,
        context_hash: str,
    ) -> None:
        """Insert or replace the metadata package (history of edits is not kept)."""
        now = utc_now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO publish_metadata (project_id, doc_json, provenance_json, model, "
                "context_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET doc_json = excluded.doc_json, "
                "provenance_json = excluded.provenance_json, model = excluded.model, "
                "context_hash = excluded.context_hash, updated_at = excluded.updated_at",
                (
                    project_id,
                    json.dumps(metadata.to_dict(), ensure_ascii=False),
                    json.dumps(provenance),
                    model,
                    context_hash,
                    now,
                    now,
                ),
            )

    # ------------------------------------------------------------------ content reviews

    def add_review(
        self,
        project_id: int,
        *,
        status: str,
        risk_level: str,
        model: str,
        context_hash: str,
        flags: tuple[SafetyFlag, ...] = (),
        summary: str = "",
        notes: tuple[str, ...] = (),
        note: str = "",
    ) -> int:
        """Append a review and return its id."""
        result = {"flags": [f.to_dict() for f in flags], "summary": summary, "notes": list(notes)}
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO safety_reviews (project_id, status, risk_level, model, "
                "context_hash, result_json, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    status,
                    risk_level,
                    model,
                    context_hash,
                    json.dumps(result, ensure_ascii=False),
                    note,
                    utc_now(),
                ),
            )
        return int(cur.lastrowid or 0)

    def reviews(self, project_id: int) -> list[StoredReview]:
        """Every review for a Short, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM safety_reviews WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()
        return [_review(r) for r in rows]

    def latest_review(self, project_id: int) -> StoredReview | None:
        """The newest review, or None."""
        row = self.conn.execute(
            "SELECT * FROM safety_reviews WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return _review(row) if row else None

    def resolve_flag(self, project_id: int, flag_key: str, action: str, note: str) -> None:
        """Record the owner's answer to a flag."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO safety_resolutions (project_id, flag_key, action, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, flag_key, action, note, utc_now()),
            )

    def resolutions(self, project_id: int) -> dict[str, Resolution]:
        """Current answer per flag key; a 'reopened' answer counts as none."""
        rows = self.conn.execute(
            "SELECT flag_key, action, note, created_at FROM safety_resolutions "
            "WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        current: dict[str, Resolution] = {}
        for r in rows:
            current[r["flag_key"]] = Resolution(
                r["flag_key"], r["action"], r["note"], r["created_at"]
            )
        return {k: v for k, v in current.items() if v.action != "reopened"}

    def resolution_history(self, project_id: int) -> list[Resolution]:
        """All answers ever recorded, oldest first."""
        rows = self.conn.execute(
            "SELECT flag_key, action, note, created_at FROM safety_resolutions "
            "WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return [Resolution(r["flag_key"], r["action"], r["note"], r["created_at"]) for r in rows]

    # ------------------------------------------------------------------ rights

    def get_rights(self, video_id: int) -> Rights | None:
        """Saved rights for a source video, or None when the owner never recorded any."""
        row = self.conn.execute(
            "SELECT rights_json, updated_at FROM source_rights WHERE video_id = ?", (video_id,)
        ).fetchone()
        if row is None:
            return None
        raw = _load(row["rights_json"])
        known = {f.name for f in fields(Rights)}
        kwargs: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        kwargs["updated_at"] = row["updated_at"]
        return Rights(**kwargs)

    def save_rights(self, video_id: int, rights: Rights) -> None:
        """Save rights and append an audit entry listing what changed."""
        old = self.get_rights(video_id)
        now = utc_now()
        stored = {k: v for k, v in rights.to_dict().items() if k != "updated_at"}
        changes: dict[str, list[str]] = {}
        previous = old or Rights()
        for name in (*HISTORY_FIELDS, "attribution_required", "attribution_text", "user_confirmed"):
            before, after = str(getattr(previous, name)), str(getattr(rights, name))
            if before != after:
                changes[name] = [before[:500], after[:500]]
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO source_rights (video_id, rights_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (video_id) DO UPDATE SET rights_json = excluded.rights_json, "
                "updated_at = excluded.updated_at",
                (video_id, json.dumps(stored, ensure_ascii=False), now),
            )
            if changes or old is None:
                self.conn.execute(
                    "INSERT INTO source_rights_history (video_id, old_status, new_status, "
                    "changes_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        video_id,
                        old.status if old else None,
                        rights.status,
                        json.dumps(changes, ensure_ascii=False),
                        now,
                    ),
                )

    def rights_history(self, video_id: int) -> list[RightsChange]:
        """Audit trail for a source video, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM source_rights_history WHERE video_id = ? ORDER BY id", (video_id,)
        ).fetchall()
        return [
            RightsChange(
                r["id"],
                r["old_status"],
                r["new_status"],
                {str(k): [str(x) for x in v] for k, v in _load(r["changes_json"]).items()},
                r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ readiness

    def get_readiness(self, project_id: int) -> StoredReadiness | None:
        """Saved checklist state, or None."""
        row = self.conn.execute(
            "SELECT * FROM publish_readiness WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return StoredReadiness(
            project_id=row["project_id"],
            checks=frozenset(str(k) for k in json.loads(row["checks_json"])),
            settings={str(k): str(v) for k, v in _load(row["settings_json"]).items()},
            rights_ack=bool(row["rights_ack"]),
            confirmed_at=row["confirmed_at"],
            confirmed_render_id=row["confirmed_render_id"],
            confirmed_fingerprint=row["confirmed_fingerprint"],
            updated_at=row["updated_at"],
        )

    def save_readiness(
        self,
        project_id: int,
        *,
        checks: frozenset[str],
        settings: dict[str, str],
        rights_ack: bool,
        confirmed_at: str | None,
        confirmed_render_id: int | None,
        confirmed_fingerprint: str | None,
    ) -> None:
        """Insert or replace the checklist state."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO publish_readiness (project_id, checks_json, settings_json, "
                "rights_ack, confirmed_at, confirmed_render_id, confirmed_fingerprint, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id) DO UPDATE SET checks_json = excluded.checks_json, "
                "settings_json = excluded.settings_json, rights_ack = excluded.rights_ack, "
                "confirmed_at = excluded.confirmed_at, "
                "confirmed_render_id = excluded.confirmed_render_id, "
                "confirmed_fingerprint = excluded.confirmed_fingerprint, "
                "updated_at = excluded.updated_at",
                (
                    project_id,
                    json.dumps(sorted(checks)),
                    json.dumps(settings),
                    int(rights_ack),
                    confirmed_at,
                    confirmed_render_id,
                    confirmed_fingerprint,
                    utc_now(),
                ),
            )

    def add_event(self, project_id: int, event: str, detail: str = "") -> None:
        """Append to the readiness history."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO publish_events (project_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, event, detail[:500], utc_now()),
            )

    def events(self, project_id: int, limit: int = 50) -> list[tuple[str, str, str]]:
        """Newest-first ``(event, detail, time)``."""
        rows = self.conn.execute(
            "SELECT event, detail, created_at FROM publish_events WHERE project_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [(r["event"], r["detail"], r["created_at"]) for r in rows]


def _load(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _review(row: sqlite3.Row) -> StoredReview:
    result = _load(row["result_json"])
    raw_flags = result.get("flags")
    flags = tuple(
        SafetyFlag.from_dict(f) for f in (raw_flags if isinstance(raw_flags, list) else [])
    )
    notes = result.get("notes")
    return StoredReview(
        id=row["id"],
        project_id=row["project_id"],
        status=row["status"],
        risk_level=row["risk_level"],
        model=row["model"],
        context_hash=row["context_hash"],
        flags=flags,
        summary=str(result.get("summary", "")),
        notes=tuple(str(n) for n in notes) if isinstance(notes, list) else (),
        note=row["note"],
        created_at=row["created_at"],
    )
