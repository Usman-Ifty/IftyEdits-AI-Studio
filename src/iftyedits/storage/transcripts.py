"""Persistence and search for transcripts, their segments and word timings.

A transcript and all its segments and words are written in one transaction, after the
job's ownership has been checked in that same transaction, so a stale or cancelled
worker can never leave a partial or outdated transcript behind.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from iftyedits.core.models import (
    SEARCH_MATCH_END,
    SEARCH_MATCH_START,
    StoredSegment,
    Transcript,
    TranscriptSearchHit,
    TranscriptSegment,
    TranscriptWord,
)
from iftyedits.storage.db import transaction, utc_now
from iftyedits.storage.processing import assert_job_owned

_COLUMNS = (
    "id, video_id, job_id, audio_sha256, engine, engine_version, model, model_revision, "
    "device, compute_type, language_requested, language, language_probability, duration_s, "
    "params_hash, params_json, segment_count, word_count, dropped_segments, processing_s, "
    "is_current, created_at"
)
_SEGMENT_COLUMNS = "id, transcript_id, position, start_s, end_s, text, avg_logprob, no_speech_prob"
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
MAX_SEARCH_TERMS = 12


def _transcript(row: sqlite3.Row) -> Transcript:
    return Transcript(
        id=row["id"],
        video_id=row["video_id"],
        job_id=row["job_id"],
        audio_sha256=row["audio_sha256"],
        engine=row["engine"],
        engine_version=row["engine_version"],
        model=row["model"],
        model_revision=row["model_revision"],
        device=row["device"],
        compute_type=row["compute_type"],
        language_requested=row["language_requested"],
        language=row["language"],
        language_probability=row["language_probability"],
        duration_s=row["duration_s"],
        params_hash=row["params_hash"],
        params=json.loads(row["params_json"]),
        segment_count=row["segment_count"],
        word_count=row["word_count"],
        dropped_segments=row["dropped_segments"],
        processing_s=row["processing_s"],
        is_current=bool(row["is_current"]),
        created_at=row["created_at"],
    )


def _segment(row: sqlite3.Row, words: tuple[TranscriptWord, ...] = ()) -> StoredSegment:
    return StoredSegment(
        id=row["id"],
        transcript_id=row["transcript_id"],
        position=row["position"],
        start_s=row["start_s"],
        end_s=row["end_s"],
        text=row["text"],
        avg_logprob=row["avg_logprob"],
        no_speech_prob=row["no_speech_prob"],
        words=words,
    )


def match_expression(query: str) -> str | None:
    """Safe FTS5 query: every word must appear, each as a prefix. None if no words.

    User input is never passed to FTS5 directly, so quotes, ``AND``, ``*`` or ``:``
    cannot cause syntax errors or column filters.
    """
    terms = _TOKEN_RE.findall(query)[:MAX_SEARCH_TERMS]
    if not terms:
        return None
    return " ".join(f'"{term}"*' for term in terms)


@dataclass(frozen=True, slots=True)
class NewTranscript:
    """Everything needed to store a finished transcript."""

    video_id: int
    audio_sha256: str
    engine: str
    engine_version: str
    model: str
    model_revision: str | None
    device: str
    compute_type: str
    language_requested: str | None
    language: str | None
    language_probability: float | None
    duration_s: float | None
    params_hash: str
    params: dict[str, Any]
    segments: Sequence[TranscriptSegment]
    dropped_segments: int = 0
    processing_s: float | None = None


class TranscriptRepository:
    """Reads, writes and searches transcripts."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ writes

    def save(
        self,
        new: NewTranscript,
        *,
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> Transcript:
        """Store a transcript and make it the video's current one.

        An existing transcript of the same video with the same parameter hash is
        replaced (with its segments and words). Raises ``JobOwnershipError`` if the job
        is no longer running on this worker; nothing is written then.
        """
        word_count = sum(len(s.words) for s in new.segments)
        with transaction(self.conn):
            assert_job_owned(self.conn, job_id, worker_id)
            self.conn.execute(
                "DELETE FROM transcripts WHERE video_id = ? AND params_hash = ?",
                (new.video_id, new.params_hash),
            )
            self.conn.execute(
                "UPDATE transcripts SET is_current = 0 WHERE video_id = ? AND is_current = 1",
                (new.video_id,),
            )
            cursor = self.conn.execute(
                "INSERT INTO transcripts (video_id, job_id, audio_sha256, engine, engine_version, "
                "model, model_revision, device, compute_type, language_requested, language, "
                "language_probability, duration_s, params_hash, params_json, segment_count, "
                "word_count, dropped_segments, processing_s, is_current, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    new.video_id,
                    job_id,
                    new.audio_sha256,
                    new.engine,
                    new.engine_version,
                    new.model,
                    new.model_revision,
                    new.device,
                    new.compute_type,
                    new.language_requested,
                    new.language,
                    new.language_probability,
                    new.duration_s,
                    new.params_hash,
                    json.dumps(new.params, sort_keys=True),
                    len(new.segments),
                    word_count,
                    new.dropped_segments,
                    new.processing_s,
                    utc_now(),
                ),
            )
            transcript_id = cursor.lastrowid
            if transcript_id is None:
                raise RuntimeError("Transcript insert returned no id")
            for position, segment in enumerate(new.segments):
                seg_cursor = self.conn.execute(
                    "INSERT INTO transcript_segments (transcript_id, position, start_s, end_s, "
                    "text, avg_logprob, no_speech_prob) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        transcript_id,
                        position,
                        segment.start_s,
                        segment.end_s,
                        segment.text,
                        segment.avg_logprob,
                        segment.no_speech_prob,
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO transcript_words (segment_id, position, start_s, end_s, text, "
                    "probability) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (seg_cursor.lastrowid, i, w.start_s, w.end_s, w.text, w.probability)
                        for i, w in enumerate(segment.words)
                    ],
                )
        saved = self.get(transcript_id)
        if saved is None:
            raise RuntimeError("Transcript vanished after insert")
        return saved

    def make_current(
        self, transcript_id: int, *, job_id: int | None = None, worker_id: str | None = None
    ) -> Transcript:
        """Make an existing transcript the current one for its video."""
        with transaction(self.conn):
            assert_job_owned(self.conn, job_id, worker_id)
            row = self.conn.execute(
                "SELECT video_id FROM transcripts WHERE id = ?", (transcript_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No transcript with id {transcript_id}")
            self.conn.execute(
                "UPDATE transcripts SET is_current = 0 WHERE video_id = ? AND is_current = 1",
                (row["video_id"],),
            )
            self.conn.execute(
                "UPDATE transcripts SET is_current = 1 WHERE id = ?", (transcript_id,)
            )
        current = self.get(transcript_id)
        if current is None:
            raise RuntimeError("Transcript vanished")
        return current

    # ------------------------------------------------------------------ reads

    def get(self, transcript_id: int) -> Transcript | None:
        """One transcript."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM transcripts WHERE id = ?",  # noqa: S608 - constant columns
            (transcript_id,),
        ).fetchone()
        return _transcript(row) if row else None

    def current(self, video_id: int) -> Transcript | None:
        """The transcript later pipeline stages should use for a video."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM transcripts WHERE video_id = ? AND is_current = 1",  # noqa: S608
            (video_id,),
        ).fetchone()
        return _transcript(row) if row else None

    def find(self, video_id: int, params_hash: str) -> Transcript | None:
        """The transcript made from these exact parameters, if any."""
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM transcripts WHERE video_id = ? AND params_hash = ?",  # noqa: S608
            (video_id, params_hash),
        ).fetchone()
        return _transcript(row) if row else None

    def for_video(self, video_id: int) -> list[Transcript]:
        """All transcripts of a video, newest first."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM transcripts WHERE video_id = ? ORDER BY id DESC",  # noqa: S608
            (video_id,),
        ).fetchall()
        return [_transcript(r) for r in rows]

    def current_by_video(self) -> dict[int, Transcript]:
        """Current transcript of every video that has one."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM transcripts WHERE is_current = 1"  # noqa: S608
        ).fetchall()
        return {r["video_id"]: _transcript(r) for r in rows}

    def segments(self, transcript_id: int, *, with_words: bool = True) -> list[StoredSegment]:
        """Segments in order, optionally with their words."""
        rows = self.conn.execute(
            f"SELECT {_SEGMENT_COLUMNS} FROM transcript_segments "  # noqa: S608
            "WHERE transcript_id = ? ORDER BY position",
            (transcript_id,),
        ).fetchall()
        words: dict[int, list[TranscriptWord]] = {}
        if with_words:
            for w in self.conn.execute(
                "SELECT w.segment_id, w.start_s, w.end_s, w.text, w.probability "
                "FROM transcript_words w JOIN transcript_segments s ON s.id = w.segment_id "
                "WHERE s.transcript_id = ? ORDER BY w.segment_id, w.position",
                (transcript_id,),
            ):
                words.setdefault(w["segment_id"], []).append(
                    TranscriptWord(w["start_s"], w["end_s"], w["text"], w["probability"])
                )
        return [_segment(r, tuple(words.get(r["id"], ()))) for r in rows]

    def search(
        self, query: str, *, video_id: int | None = None, limit: int = 50
    ) -> list[TranscriptSearchHit]:
        """Segments of current transcripts containing every word of ``query`` (as prefixes).

        Matching ignores case and accents. Best matches first.
        """
        expression = match_expression(query)
        if expression is None:
            return []
        sql = (
            "SELECT s.id AS segment_id, s.transcript_id, s.position, s.start_s, s.end_s, "
            "s.text, t.video_id, v.original_filename, "
            "snippet(transcript_search, 0, ?, ?, '…', 16) AS snippet "
            "FROM transcript_search "
            "JOIN transcript_segments s ON s.id = transcript_search.rowid "
            "JOIN transcripts t ON t.id = s.transcript_id "
            "JOIN videos v ON v.id = t.video_id "
            "WHERE transcript_search MATCH ? AND t.is_current = 1"
        )
        params: list[Any] = [SEARCH_MATCH_START, SEARCH_MATCH_END, expression]
        if video_id is not None:
            sql += " AND t.video_id = ?"
            params.append(video_id)
        sql += " ORDER BY rank, t.video_id, s.position LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            TranscriptSearchHit(
                video_id=r["video_id"],
                original_filename=r["original_filename"],
                transcript_id=r["transcript_id"],
                segment_id=r["segment_id"],
                position=r["position"],
                start_s=r["start_s"],
                end_s=r["end_s"],
                text=r["text"],
                snippet=r["snippet"],
            )
            for r in rows
        ]
