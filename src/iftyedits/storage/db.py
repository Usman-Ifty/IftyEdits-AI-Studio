"""SQLite connection handling and numbered SQL migrations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(?P<number>\d{4})_[a-z0-9_]+\.sql$")


def utc_now() -> str:
    """Current UTC time as an ISO 8601 string with microseconds."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in autocommit mode with WAL and foreign keys enabled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def open_database(path: Path) -> sqlite3.Connection:
    """Connect and apply pending migrations."""
    conn = connect(path)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in an ``IMMEDIATE`` transaction; roll back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations() -> list[tuple[int, str, str]]:
    """Return ``(number, filename, sql)`` for bundled migrations, in order."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("iftyedits.storage.migrations").iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match["number"]), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    numbers = [n for n, _, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"Migration numbers must be contiguous from 0001: {numbers}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    """Current ``PRAGMA user_version``."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations atomically, one transaction each. Returns the new version."""
    current = schema_version(conn)
    for number, name, sql in available_migrations():
        if number <= current:
            continue
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise RuntimeError(f"Migration {name} failed: {exc}") from exc
        current = number
    return current
