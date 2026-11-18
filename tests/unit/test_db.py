"""SQLite connection, migrations and the video repository."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from iftyedits.adapters.ffprobe import parse_probe_json
from iftyedits.core.models import Video, VideoStatus
from iftyedits.storage import db
from iftyedits.storage.videos import VideoRepository

PROBE = json.dumps(
    {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
        "format": {"format_name": "mp4", "duration": "2.0", "bit_rate": "1000"},
    }
)
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.open_database(tmp_path / "db" / "test.sqlite")


def test_migrations_apply_and_are_idempotent(conn: sqlite3.Connection) -> None:
    latest = len(db.available_migrations())
    assert db.schema_version(conn) == latest
    assert db.migrate(conn) == latest
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"videos", "video_sources", "media_streams", "jobs", "workers"} <= tables


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_transaction_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), db.transaction(conn):
        conn.execute(
            "INSERT INTO workers (worker_id, pid, started_at, last_seen) VALUES ('w', 1, 'x', 'x')"
        )
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM workers").fetchone()[0] == 0


def test_record_import_creates_video_streams_and_source(conn: sqlite3.Connection) -> None:
    repo = VideoRepository(conn)
    video, created = repo.record_import(
        sha256=SHA_A,
        vault_relpath=f"aa/{SHA_A}.mp4",
        original_filename="clip.mp4",
        size_bytes=1234,
        info=parse_probe_json(PROBE),
        probe_error=None,
        source_path=Path("/Users/me/clip.mp4"),
    )
    assert created
    assert video.status is VideoStatus.READY
    assert video.duration_s == 2.0
    assert [s.codec_type for s in repo.streams(video.id)] == ["video", "audio"]
    assert [s.source_path for s in repo.sources(video.id)] == ["/Users/me/clip.mp4"]
    assert repo.get_by_sha(SHA_A) == video
    assert repo.find_by_size(1234) == [video]
    assert repo.known_hashes() == {SHA_A}


def test_duplicate_content_adds_source_only(conn: sqlite3.Connection) -> None:
    repo = VideoRepository(conn)

    def record(name: str, source: str) -> tuple[Video, bool]:
        return repo.record_import(
            sha256=SHA_A,
            vault_relpath=f"aa/{SHA_A}.mp4",
            original_filename=name,
            size_bytes=1,
            info=parse_probe_json(PROBE),
            probe_error=None,
            source_path=Path(source),
        )

    first, _ = record("a.mp4", "/x/a.mp4")
    second, created = record("b.MOV", "/y/b.MOV")
    again, _ = record("b.MOV", "/y/b.MOV")
    assert not created
    assert second.id == first.id == again.id
    assert second.original_filename == "a.mp4"
    assert len(repo.streams(first.id)) == 2
    assert [s.source_path for s in repo.sources(first.id)] == ["/x/a.mp4", "/y/b.MOV"]


def test_probe_failed_status(conn: sqlite3.Connection) -> None:
    repo = VideoRepository(conn)
    video, _ = repo.record_import(
        sha256=SHA_B,
        vault_relpath=f"bb/{SHA_B}",
        original_filename="odd.mkv",
        size_bytes=5,
        info=None,
        probe_error="bad header",
        source_path=None,
    )
    assert video.status is VideoStatus.PROBE_FAILED
    assert video.probe_error == "bad header"
    assert repo.sources(video.id) == []


def test_list_recent_orders_newest_first(conn: sqlite3.Connection) -> None:
    repo = VideoRepository(conn)
    for sha in (SHA_A, SHA_B):
        repo.record_import(
            sha256=sha,
            vault_relpath=f"{sha[:2]}/{sha}",
            original_filename=f"{sha[0]}.mp4",
            size_bytes=1,
            info=None,
            probe_error="x",
            source_path=None,
        )
    assert [v.sha256 for v in repo.list_recent()] == [SHA_B, SHA_A]


def test_invalid_sha_rejected_by_schema(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        VideoRepository(conn).record_import(
            sha256="short",
            vault_relpath="x",
            original_filename="x",
            size_bytes=1,
            info=None,
            probe_error=None,
            source_path=None,
        )
