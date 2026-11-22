"""Persistence for AI provider choices and usage records (no secrets, no prompt text)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from iftyedits.core.usage import Usage, UsageTotals
from iftyedits.storage.db import utc_now


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One recorded model request."""

    created_at: str
    provider: str
    model: str
    feature: str
    input_tokens: int | None
    output_tokens: int | None
    ok: bool
    note: str


class AiRepository:
    """Settings overrides and usage rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_setting(self, key: str) -> str | None:
        """The stored value, or None."""
        row = self.conn.execute("SELECT value FROM ai_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Store a non-secret choice."""
        self.conn.execute(
            "INSERT INTO ai_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, utc_now()),
        )

    def add_usage(
        self,
        *,
        provider: str,
        model: str,
        feature: str,
        usage: Usage | None,
        ok: bool,
        note: str = "",
        at: str | None = None,
    ) -> None:
        """Record one request."""
        self.conn.execute(
            "INSERT INTO ai_usage (created_at, provider, model, feature, input_tokens, "
            "output_tokens, ok, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                at or utc_now(),
                provider,
                model,
                feature,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                1 if ok else 0,
                note[:200],
            ),
        )

    def totals(self, provider: str, month: str) -> UsageTotals:
        """Sums for one provider in a month (``YYYY-MM``)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS i, "
            "COALESCE(SUM(output_tokens), 0) AS o, "
            "COALESCE(SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL "
            "THEN 1 ELSE 0 END), 0) AS missing "
            "FROM ai_usage WHERE provider = ? AND substr(created_at, 1, 7) = ?",
            (provider, month),
        ).fetchone()
        return UsageTotals(int(row["calls"]), int(row["i"]), int(row["o"]), int(row["missing"]))

    def by_feature(self, provider: str, month: str) -> list[tuple[str, UsageTotals]]:
        """Per-feature sums for one provider in a month."""
        rows = self.conn.execute(
            "SELECT feature, COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS i, "
            "COALESCE(SUM(output_tokens), 0) AS o FROM ai_usage "
            "WHERE provider = ? AND substr(created_at, 1, 7) = ? GROUP BY feature "
            "ORDER BY feature",
            (provider, month),
        ).fetchall()
        return [
            (
                str(r["feature"]) or "(unlabelled)",
                UsageTotals(int(r["calls"]), int(r["i"]), int(r["o"])),
            )
            for r in rows
        ]

    def recent(self, limit: int = 10) -> list[UsageRow]:
        """The newest requests first."""
        rows = self.conn.execute(
            "SELECT * FROM ai_usage ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            UsageRow(
                r["created_at"],
                r["provider"],
                r["model"],
                r["feature"],
                r["input_tokens"],
                r["output_tokens"],
                bool(r["ok"]),
                r["note"],
            )
            for r in rows
        ]
