"""Migration 0003 and the processing-stage / artifact repository."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iftyedits.core.errors import JobOwnershipError
from iftyedits.core.models import Artifact, ArtifactKind, JobStatus, Stage, StageStatus
from iftyedits.pipeline.jobs import IMPORT_FILE, JobQueue
from iftyedits.storage import db
from iftyedits.storage.processing import ProcessingRepository

SHA = "c" * 64
DIGEST = "d" * 64


def _v2_database(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    for number, _, sql in db.available_migrations()[:2]:
        conn.executescript(f"BEGIN; {sql}\nPRAGMA user_version = {number}; COMMIT;")
    assert db.schema_version(conn) == 2
    return conn


def _seed_v2(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO videos (sha256, vault_relpath, original_filename, size_bytes, status, "
        "imported_at) VALUES (?, 'cc/x.mp4', 'x.mp4', 10, 'ready', 'now')",
        (SHA,),
    )
    rows = [
        (5, "queued", None, None),
        (6, "running", "w1", None),
        (9, "succeeded", None, '{"outcome": "imported"}'),
        (12, "failed", None, None),
    ]
    for job_id, status, worker, result in rows:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, status, progress, message, error, result, "
            "attempts, created_at, worker_id) VALUES (?, 'import_file', '{\"path\": \"/a\"}', "
            "?, 0.5, 'msg', 'err', ?, 2, 'created', ?)",
            (job_id, status, result, worker),
        )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.open_database(tmp_path / "db.sqlite")


@pytest.fixture
def video_id(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO videos (sha256, vault_relpath, original_filename, size_bytes, status, "
        "imported_at) VALUES (?, 'cc/x.mp4', 'x.mp4', 10, 'ready', 'now')",
        (SHA,),
    )
    return 1


# ------------------------------------------------------------------ migration


def test_v2_to_v3_preserves_jobs_and_videos(tmp_path: Path) -> None:
    conn = _v2_database(tmp_path / "old.sqlite")
    _seed_v2(conn)
    before = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    assert db.migrate(conn) == 10  # through v3 to the latest schema (v10 adds YouTube uploads)
    after = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    assert [tuple(r)[:14] for r in after] == [tuple(r) for r in before]
    assert all(r["cancel_requested_at"] is None for r in after)
    assert conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 1

    queue = JobQueue(conn)
    job = queue.get(6)
    assert job is not None and job.status is JobStatus.RUNNING and job.worker_id == "w1"
    claimed = queue.claim("new")
    assert claimed is not None and claimed.id == 5  # ids and order kept
    new_id = queue.enqueue(IMPORT_FILE, {"path": "/b"})
    assert new_id == 13
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"processing_stages", "artifacts", "jobs"} <= tables
    assert "jobs_v3" not in tables
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_jobs_status" in indexes
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_v3_allows_cancelled_and_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    insert = "INSERT INTO jobs (kind, payload, status, created_at) VALUES ('k', '{}', ?, 'now')"
    conn.execute(insert, ("cancelled",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("paused",))


def test_failed_migration_rolls_back_to_v2(tmp_path: Path) -> None:
    conn = _v2_database(tmp_path / "old.sqlite")
    _seed_v2(conn)
    conn.execute("CREATE TABLE processing_stages (id INTEGER PRIMARY KEY)")  # collides with 0003

    with pytest.raises(RuntimeError, match=r"0003_processing\.sql failed"):
        db.migrate(conn)
    assert db.schema_version(conn) == 2
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "cancel_requested_at" not in columns
    with pytest.raises(sqlite3.IntegrityError):  # old CHECK constraint still in force
        conn.execute(
            "INSERT INTO jobs (kind, payload, status, created_at) "
            "VALUES ('k', '{}', 'cancelled', 'now')"
        )
    conn.close()


# ------------------------------------------------------------------ stages


def test_stage_lifecycle_and_rerun_resets(conn: sqlite3.Connection, video_id: int) -> None:
    repo = ProcessingRepository(conn)
    record = repo.start_stage(video_id, Stage.AUDIO, params_hash="p1", params={"a": 1})
    assert record.status is StageStatus.RUNNING and record.params == {"a": 1}
    repo.update_progress(record.id, 0.4, "40%")
    repo.finish_stage(record.id, StageStatus.FAILED, error="ffmpeg failed")
    failed = repo.stage(video_id, Stage.AUDIO)
    assert failed is not None
    assert (failed.status, failed.error, failed.progress) == (
        StageStatus.FAILED,
        "ffmpeg failed",
        0.4,
    )
    assert failed.finished_at is not None

    again = repo.start_stage(video_id, Stage.AUDIO, params_hash="p2", params={"a": 2})
    assert again.id == record.id
    assert (again.status, again.error, again.progress, again.finished_at) == (
        StageStatus.RUNNING,
        None,
        0.0,
        None,
    )
    repo.finish_stage(again.id, StageStatus.SUCCEEDED, message="done")
    done = repo.stage(video_id, Stage.AUDIO)
    assert done is not None and (done.status, done.progress) == (StageStatus.SUCCEEDED, 1.0)
    with pytest.raises(ValueError, match="final"):
        repo.finish_stage(again.id, StageStatus.RUNNING)


def test_skipped_stage_and_summary(conn: sqlite3.Connection, video_id: int) -> None:
    repo = ProcessingRepository(conn)
    skipped = repo.record_skipped(
        video_id, Stage.AUDIO, params_hash="p", params={}, message="No audio stream"
    )
    assert (skipped.status, skipped.message) == (StageStatus.SKIPPED, "No audio stream")
    repo.start_stage(video_id, Stage.PROXY, params_hash="p", params={})
    assert repo.stage_summary() == {
        video_id: {Stage.AUDIO: StageStatus.SKIPPED, Stage.PROXY: StageStatus.RUNNING}
    }
    assert [s.stage for s in repo.stages(video_id)] == [Stage.AUDIO, Stage.PROXY]


def test_orphaned_running_stages_are_failed(conn: sqlite3.Connection, video_id: int) -> None:
    queue = JobQueue(conn)
    repo = ProcessingRepository(conn)
    live = queue.enqueue(IMPORT_FILE, {"path": "/live"})
    queue.claim("w")
    repo.start_stage(video_id, Stage.AUDIO, params_hash="p", params={}, job_id=live, worker_id="w")
    repo.start_stage(video_id, Stage.PROXY, params_hash="p", params={})  # no job: orphaned
    assert repo.fail_orphaned_running() == 1
    proxy = repo.stage(video_id, Stage.PROXY)
    audio = repo.stage(video_id, Stage.AUDIO)
    assert proxy is not None and proxy.status is StageStatus.FAILED
    assert proxy.error is not None and "Interrupted" in proxy.error
    assert audio is not None and audio.status is StageStatus.RUNNING


# ------------------------------------------------------------------ artifacts


def _register(
    repo: ProcessingRepository,
    video_id: int,
    relpath: str,
    job_id: int | None = None,
    worker_id: str | None = None,
) -> tuple[Artifact, str | None]:
    stage = repo.start_stage(
        video_id, Stage.PROXY, params_hash="p", params={}, job_id=job_id, worker_id=worker_id
    )
    return repo.register_artifact(
        video_id=video_id,
        stage_id=stage.id,
        kind=ArtifactKind.PROXY_MP4,
        relpath=relpath,
        size_bytes=123,
        sha256=DIGEST,
        params_hash="p",
        metadata={"width": 1280},
        job_id=job_id,
        worker_id=worker_id,
    )


def test_register_artifact_replaces_and_reports_old_path(
    conn: sqlite3.Connection, video_id: int
) -> None:
    repo = ProcessingRepository(conn)
    artifact, replaced = _register(repo, video_id, "cc/x/proxy-a.mp4")
    assert replaced is None
    assert (artifact.kind, artifact.metadata, artifact.size_bytes) == (
        ArtifactKind.PROXY_MP4,
        {"width": 1280},
        123,
    )
    _, replaced = _register(repo, video_id, "cc/x/proxy-b.mp4")
    assert replaced == "cc/x/proxy-a.mp4"
    _, same = _register(repo, video_id, "cc/x/proxy-b.mp4")
    assert same is None
    assert [a.relpath for a in repo.artifacts(video_id)] == ["cc/x/proxy-b.mp4"]
    assert repo.relpath_in_use("cc/x/proxy-b.mp4")
    assert not repo.relpath_in_use("cc/x/proxy-a.mp4")


def test_artifact_constraints(conn: sqlite3.Connection, video_id: int) -> None:
    repo = ProcessingRepository(conn)
    stage = repo.start_stage(video_id, Stage.AUDIO, params_hash="p", params={})
    for size, digest in ((0, DIGEST), (5, "short")):
        with pytest.raises(sqlite3.IntegrityError):
            repo.register_artifact(
                video_id=video_id,
                stage_id=stage.id,
                kind=ArtifactKind.AUDIO_WAV,
                relpath="x.wav",
                size_bytes=size,
                sha256=digest,
                params_hash="p",
                metadata={},
            )


def test_writes_require_job_ownership(conn: sqlite3.Connection, video_id: int) -> None:
    queue = JobQueue(conn)
    repo = ProcessingRepository(conn)
    job_id = queue.enqueue(IMPORT_FILE, {"path": "/a"})
    queue.claim("owner")
    stage = repo.start_stage(
        video_id, Stage.PROXY, params_hash="p", params={}, job_id=job_id, worker_id="owner"
    )
    with pytest.raises(JobOwnershipError):
        repo.start_stage(
            video_id, Stage.PROXY, params_hash="p", params={}, job_id=job_id, worker_id="intruder"
        )
    with pytest.raises(JobOwnershipError):
        _register(repo, video_id, "cc/x/p.mp4", job_id=job_id, worker_id="intruder")
    with pytest.raises(JobOwnershipError):
        repo.finish_stage(stage.id, StageStatus.SUCCEEDED, job_id=job_id, worker_id="intruder")
    assert repo.artifacts(video_id) == []

    queue.succeed(job_id, {}, worker_id="owner")
    with pytest.raises(JobOwnershipError):  # finished jobs cannot write either
        repo.finish_stage(stage.id, StageStatus.SUCCEEDED, job_id=job_id, worker_id="owner")
    current = repo.stage(video_id, Stage.PROXY)
    assert current is not None and current.status is StageStatus.RUNNING
