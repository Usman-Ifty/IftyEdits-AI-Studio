"""Transcript persistence: migration 0004, replace/current rules, ownership, search."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from iftyedits.core.errors import JobOwnershipError
from iftyedits.core.models import SEARCH_MATCH_END, SEARCH_MATCH_START, Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.db import connect, migrate, schema_version
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository, match_expression
from tests.conftest import MakeMedia
from tests.transcription_fakes import DEFAULT_SEGMENTS, segment


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Podcast 12.mp4")).video


def _new(video_id: int, params_hash: str = "p1", **changes: object) -> NewTranscript:
    base = NewTranscript(
        video_id=video_id,
        audio_sha256="b" * 64,
        engine="fake-engine",
        engine_version="1",
        model="small",
        model_revision="rev",
        device="cpu",
        compute_type="int8",
        language_requested=None,
        language="en",
        language_probability=0.95,
        duration_s=2.0,
        params_hash=params_hash,
        params={"model": "small"},
        segments=DEFAULT_SEGMENTS,
        processing_s=1.5,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_migration_creates_tables(db_conn: sqlite3.Connection) -> None:
    assert schema_version(db_conn) >= 4
    tables = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master")}
    assert {"transcripts", "transcript_segments", "transcript_words", "transcript_search"} <= tables


def test_migration_upgrades_a_v3_database(tmp_path: object) -> None:
    from pathlib import Path

    conn = connect(Path(str(tmp_path)) / "old.sqlite")
    from iftyedits.storage.db import available_migrations

    for number, _, sql in available_migrations()[:3]:
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    assert schema_version(conn) == 3
    assert migrate(conn) >= 4
    assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    conn.close()


def test_save_and_read_back(db_conn: sqlite3.Connection, video: Video) -> None:
    repo = TranscriptRepository(db_conn)
    saved = repo.save(_new(video.id))
    assert saved.is_current and saved.segment_count == 2 and saved.word_count == 5
    assert repo.current(video.id) == saved
    segments = repo.segments(saved.id)
    assert [s.position for s in segments] == [0, 1]
    assert segments[1].text == " Money talks, café!"
    assert [w.text for w in segments[1].words] == [" Money", " talks,", " café!"]
    assert repo.segments(saved.id, with_words=False)[1].words == ()
    assert repo.find(video.id, "p1") == saved


def test_same_parameters_replace_other_parameters_add(
    db_conn: sqlite3.Connection, video: Video
) -> None:
    repo = TranscriptRepository(db_conn)
    first = repo.save(_new(video.id, "p1"))
    second = repo.save(_new(video.id, "p1", segments=(segment(0, 1, "only one"),)))
    assert repo.get(first.id) is None
    assert [t.id for t in repo.for_video(video.id)] == [second.id]
    words = db_conn.execute("SELECT count(*) FROM transcript_words").fetchone()[0]
    assert words == 0  # words of the replaced transcript were removed with it

    other = repo.save(_new(video.id, "p2", model="tiny"))
    versions = repo.for_video(video.id)
    assert [t.id for t in versions] == [other.id, second.id]
    assert [t.is_current for t in versions] == [True, False]
    assert repo.make_current(second.id).is_current
    assert repo.current(video.id) == repo.get(second.id)
    with pytest.raises(KeyError):
        repo.make_current(9999)


def test_only_one_current_transcript_per_video(db_conn: sqlite3.Connection, video: Video) -> None:
    repo = TranscriptRepository(db_conn)
    a = repo.save(_new(video.id, "p1"))
    repo.save(_new(video.id, "p2"))
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute("UPDATE transcripts SET is_current = 1 WHERE id = ?", (a.id,))


def test_constraints_reject_invalid_timings(db_conn: sqlite3.Connection, video: Video) -> None:
    saved = TranscriptRepository(db_conn).save(_new(video.id))
    for start, end, text in ((-1.0, 1.0, "x"), (2.0, 1.0, "x"), (0.0, 1.0, "")):
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO transcript_segments (transcript_id, position, start_s, end_s, text) "
                "VALUES (?, 99, ?, ?, ?)",
                (saved.id, start, end, text),
            )


def test_save_requires_ownership(db_conn: sqlite3.Connection, video: Video) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    claimed = queue.claim("w1")
    assert claimed is not None and claimed.id == job_id
    repo = TranscriptRepository(db_conn)
    with pytest.raises(JobOwnershipError):
        repo.save(_new(video.id), job_id=job_id, worker_id="someone-else")
    assert repo.for_video(video.id) == []
    saved = repo.save(_new(video.id), job_id=job_id, worker_id="w1")
    assert saved.job_id == job_id


def test_deleting_video_cascades_and_cleans_search(
    db_conn: sqlite3.Connection, video: Video
) -> None:
    repo = TranscriptRepository(db_conn)
    repo.save(_new(video.id))
    assert repo.search("money")
    db_conn.execute("DELETE FROM videos WHERE id = ?", (video.id,))
    for table in ("transcripts", "transcript_segments", "transcript_words"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert repo.search("money") == []


def test_search_matching_rules(db_conn: sqlite3.Connection, video: Video) -> None:
    repo = TranscriptRepository(db_conn)
    repo.save(_new(video.id))
    hits = repo.search("MONEY")
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.video_id, hit.position, hit.start_s) == (video.id, 1, 0.9)
    assert hit.original_filename == "Podcast 12.mp4"
    assert f"{SEARCH_MATCH_START}Money{SEARCH_MATCH_END}" in hit.snippet
    assert len(repo.search("cafe")) == 1  # accents ignored
    assert len(repo.search("tal")) == 1  # prefix match
    assert repo.search("money hello") == []  # every word must be in the same segment
    assert repo.search("") == [] and repo.search("  ?!  ") == []
    assert repo.search('"; DROP TABLE videos; --') == []
    assert repo.search("money", video_id=video.id + 1) == []
    assert repo.search("money OR hello") == []  # OR is a word, not an operator


def test_search_only_uses_current_transcripts(db_conn: sqlite3.Connection, video: Video) -> None:
    repo = TranscriptRepository(db_conn)
    old = repo.save(_new(video.id, "p1"))
    repo.save(_new(video.id, "p2", segments=(segment(0, 1, "completely different"),)))
    assert repo.search("money") == []
    repo.make_current(old.id)
    assert len(repo.search("money")) == 1


def test_match_expression_quotes_terms() -> None:
    assert match_expression('foo "bar" baz*') == '"foo"* "bar"* "baz"*'
    assert match_expression("---") is None
    assert match_expression("ﺷﻜﺮﺍ 日本語") == '"ﺷﻜﺮﺍ"* "日本語"*'
