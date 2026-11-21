"""Clip persistence: migration 0005, atomic save, reuse, decisions, feedback, cascade."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iftyedits.core.clip_models import ClipDecision
from iftyedits.core.errors import ClipAnalysisError, ClipNotFoundError, JobOwnershipError
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage import db
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.clip_fakes import new_analysis, podcast
from tests.conftest import MakeMedia
from tests.transcription_fakes import DEFAULT_SEGMENTS


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Podcast 12.mp4")).video


@pytest.fixture
def transcript_id(db_conn: sqlite3.Connection, video: Video) -> int:
    saved = TranscriptRepository(db_conn).save(
        NewTranscript(
            video_id=video.id,
            audio_sha256="b" * 64,
            engine="fake",
            engine_version="1",
            model="small",
            model_revision="r",
            device="cpu",
            compute_type="int8",
            language_requested=None,
            language="en",
            language_probability=0.9,
            duration_s=2.0,
            params_hash="t1",
            params={},
            segments=DEFAULT_SEGMENTS,
        )
    )
    return saved.id


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> ClipRepository:
    return ClipRepository(db_conn)


def test_migration_creates_tables(db_conn: sqlite3.Connection) -> None:
    assert db.schema_version(db_conn) >= 5
    tables = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master")}
    assert {"clip_analyses", "clip_signals", "clip_candidates", "clip_decisions"} <= tables


def test_v4_database_upgrades(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "old.sqlite")
    for number, _, sql in db.available_migrations()[:4]:
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    assert db.schema_version(conn) == 4
    assert db.migrate(conn) >= 5
    assert conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0
    conn.close()


def test_save_and_read_back(repo: ClipRepository, video: Video, transcript_id: int) -> None:
    new = new_analysis(video.sha256, video.id, transcript_id)
    saved = repo.save(new)
    assert saved.is_current and saved.candidate_count == len(new.candidates) >= 2
    assert saved.signal_count == len(new.signals) > 0
    assert repo.current(video.id) == saved and repo.find(video.id, "h1") == saved
    assert repo.current_by_video() == {video.id: saved}

    candidates = repo.candidates(saved.id)
    assert [c.rank for c in candidates] == list(range(1, len(candidates) + 1))
    assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)
    first, original = candidates[0], new.candidates[0][1]
    assert first.key == new.candidates[0][0] and len(first.key) == 16
    assert (first.start_s, first.end_s, first.category) == (
        original.start_s,
        original.end_s,
        original.category,
    )
    assert first.components == original.components
    assert first.hook == original.hook and first.payoff == original.payoff
    assert first.strengths == original.strengths and first.weaknesses == original.weaknesses
    assert first.decision is None and first.duration_s == pytest.approx(
        original.end_s - original.start_s
    )
    assert repo.candidate(first.id) == first
    assert repo.candidate_by_key(video.id, first.key) == first

    stored = repo.signals(saved.id)
    assert [s.position for s in stored] == list(range(len(stored)))
    assert [(s.kind, s.start_s) for s in stored] == [(s.kind, s.start_s) for s in new.signals]


def test_same_parameters_replace_other_parameters_add(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    first = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h1"))
    again = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h1"))
    assert repo.get(first.id) is None and again.is_current
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 1
    assert db_conn.execute("SELECT count(*) FROM clip_candidates").fetchone()[0] == len(
        repo.candidates(again.id)
    )

    other = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h2"))
    assert repo.current(video.id) == other
    reloaded = repo.get(again.id)
    assert reloaded is not None and not reloaded.is_current

    back = repo.make_current(again.id)
    assert back.is_current and repo.current(video.id) == back
    assert repo.get(other.id) is not None and not repo.get(other.id).is_current  # type: ignore[union-attr]
    with pytest.raises(ClipNotFoundError):
        repo.make_current(9999)


def test_save_requires_job_ownership_and_stores_nothing_otherwise(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    claimed = queue.claim("w1")
    assert claimed is not None and claimed.id == job_id
    new = new_analysis(video.sha256, video.id, transcript_id)
    with pytest.raises(JobOwnershipError):
        repo.save(new, job_id=job_id, worker_id="someone-else")
    for table in ("clip_analyses", "clip_signals", "clip_candidates"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    with pytest.raises(JobOwnershipError):
        repo.make_current(1, job_id=job_id, worker_id="someone-else")
    assert repo.save(new, job_id=job_id, worker_id="w1").job_id == job_id


def test_a_failed_insert_rolls_back_and_keeps_the_previous_analysis(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    good = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h1"))
    broken = new_analysis(video.sha256, video.id, transcript_id, params_hash="h2")
    duplicate = broken.candidates[0]
    object.__setattr__(broken, "candidates", [*broken.candidates, duplicate])  # same key twice
    with pytest.raises(sqlite3.IntegrityError):
        repo.save(broken)
    assert repo.current(video.id) == good
    assert repo.find(video.id, "h2") is None
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 1


def test_decisions_persist_across_reanalysis(
    repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    saved = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h1"))
    a, b = repo.candidates(saved.id)[:2]
    assert repo.decide(a.id, ClipDecision.FAVORITE).decision is ClipDecision.FAVORITE
    assert repo.decide(b.id, ClipDecision.REJECTED).decision is ClipDecision.REJECTED
    assert repo.decision_counts(video.id) == {"favorite": 1, "rejected": 1}

    again = repo.save(new_analysis(video.sha256, video.id, transcript_id, params_hash="h2"))
    kept = {c.key: c for c in repo.candidates(again.id)}
    assert kept[a.key].decision is ClipDecision.FAVORITE
    assert kept[b.key].decision is ClipDecision.REJECTED

    cleared = repo.decide(a.id if repo.candidate(a.id) else kept[a.key].id, None)
    assert cleared.decision is None
    assert repo.decision_counts(video.id) == {"rejected": 1}
    with pytest.raises(ClipNotFoundError):
        repo.decide(9999, ClipDecision.FAVORITE)


def test_adjustments_validate_and_keep_the_decision(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    saved = repo.save(new_analysis(video.sha256, video.id, transcript_id))
    c = repo.candidates(saved.id)[0]
    repo.decide(c.id, ClipDecision.FAVORITE)
    adjusted = repo.set_adjustment(c.id, c.start_s + 1, c.end_s - 1)
    assert adjusted.decision is ClipDecision.FAVORITE
    assert (adjusted.adjusted_start_s, adjusted.adjusted_end_s) == (c.start_s + 1, c.end_s - 1)
    assert repo.decide(c.id, ClipDecision.REJECTED).adjusted_start_s == c.start_s + 1

    for bad in ((5.0, None), (None, 5.0), (10.0, 10.0), (-1.0, 4.0)):
        with pytest.raises(ClipAnalysisError):
            repo.set_adjustment(c.id, *bad)
    cleared = repo.set_adjustment(c.id, None, None)
    assert cleared.adjusted_start_s is None and cleared.decision is ClipDecision.REJECTED
    repo.decide(c.id, None)
    assert db_conn.execute("SELECT count(*) FROM clip_decisions").fetchone()[0] == 0


def test_adjustment_without_a_decision_is_kept_and_clearing_removes_the_row(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    saved = repo.save(new_analysis(video.sha256, video.id, transcript_id))
    c = repo.candidates(saved.id)[0]
    repo.set_adjustment(c.id, c.start_s, c.end_s - 2)
    assert db_conn.execute("SELECT count(*) FROM clip_decisions").fetchone()[0] == 1
    repo.set_adjustment(c.id, None, None)
    assert db_conn.execute("SELECT count(*) FROM clip_decisions").fetchone()[0] == 0


def test_feedback_aggregates_spans_and_category_bias(
    repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    assert repo.feedback(video.id).favorite_spans == ()
    saved = repo.save(new_analysis(video.sha256, video.id, transcript_id))
    a, b = repo.candidates(saved.id)[:2]
    repo.decide(a.id, ClipDecision.FAVORITE)
    repo.decide(b.id, ClipDecision.REJECTED)
    feedback = repo.feedback(video.id)
    assert feedback.favorite_spans == ((a.start_s, a.end_s),)
    assert feedback.rejected_spans == ((b.start_s, b.end_s),)
    assert a.category in feedback.category_bias or b.category in feedback.category_bias
    assert repo.feedback(video.id + 1).favorite_spans == ()  # spans stay with their video
    assert repo.feedback(video.id + 1).category_bias == feedback.category_bias


def test_deleting_a_video_removes_everything(
    db_conn: sqlite3.Connection, repo: ClipRepository, video: Video, transcript_id: int
) -> None:
    saved = repo.save(new_analysis(video.sha256, video.id, transcript_id))
    repo.decide(repo.candidates(saved.id)[0].id, ClipDecision.FAVORITE)
    db_conn.execute("DELETE FROM videos WHERE id = ?", (video.id,))
    for table in ("clip_analyses", "clip_signals", "clip_candidates", "clip_decisions"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_empty_analysis_is_valid(repo: ClipRepository, video: Video, transcript_id: int) -> None:
    new = new_analysis(video.sha256, video.id, transcript_id, segments=podcast()[:0] or None)
    object.__setattr__(new, "candidates", [])
    object.__setattr__(new, "signals", [])
    saved = repo.save(new)
    assert saved.candidate_count == 0 and saved.signal_count == 0
    assert repo.candidates(saved.id) == [] and repo.signals(saved.id) == []
