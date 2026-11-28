"""Persistence for clip analyses, their signals, ranked suggestions and user decisions.

An analysis, its signals and its suggestions are written in one transaction, after the
job's ownership has been checked in that same transaction, so a failed, cancelled or
stale run never leaves partial results. Decisions are stored separately, keyed by the
suggestion's stable key, so re-analysing a video keeps what the user decided.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from iftyedits.core.clip_models import (
    ClipAnalysis,
    ClipCandidate,
    ClipDecision,
    ClipSpan,
    ContentSignal,
    ScoreComponent,
    ScoredCandidate,
    SignalKind,
    SignalSource,
    StoredSignal,
)
from iftyedits.core.errors import ClipAnalysisError, ClipNotFoundError
from iftyedits.core.romanize import romanize
from iftyedits.core.scoring import Feedback, category_bias
from iftyedits.storage.db import transaction, utc_now
from iftyedits.storage.processing import assert_job_owned

_ANALYSIS_COLUMNS = (
    "id, video_id, transcript_id, job_id, analysis_version, scoring_version, params_hash, "
    "params_json, duration_s, sources_json, stats_json, signal_count, candidate_count, "
    "processing_s, is_current, created_at"
)
_CANDIDATE_COLUMNS = (
    "c.id, c.analysis_id, c.video_id, c.candidate_key, c.rank, c.start_s, c.end_s, c.category, "
    "c.methods_json, c.title, c.excerpt, c.hook_json, c.context_json, c.payoff_json, c.score, "
    "c.confidence, c.components_json, c.strengths_json, c.weaknesses_json, c.explanation, "
    "c.signal_positions_json, c.evidence_json, d.decision, d.adjusted_start_s, "
    "d.adjusted_end_s, d.decided_at"
)
_CANDIDATE_FROM = (
    "FROM clip_candidates c LEFT JOIN clip_decisions d "
    "ON d.video_id = c.video_id AND d.candidate_key = c.candidate_key"
)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _span_json(span: ClipSpan) -> str:
    return _dump([span.start_s, span.end_s, span.text])


def _span(raw: str) -> ClipSpan:
    start, end, text = json.loads(raw)
    return ClipSpan(float(start), float(end), romanize(str(text)))


def _analysis(row: sqlite3.Row) -> ClipAnalysis:
    return ClipAnalysis(
        id=row["id"],
        video_id=row["video_id"],
        transcript_id=row["transcript_id"],
        job_id=row["job_id"],
        analysis_version=row["analysis_version"],
        scoring_version=row["scoring_version"],
        params_hash=row["params_hash"],
        params=json.loads(row["params_json"]),
        duration_s=row["duration_s"],
        sources=json.loads(row["sources_json"]),
        stats=json.loads(row["stats_json"]),
        signal_count=row["signal_count"],
        candidate_count=row["candidate_count"],
        processing_s=row["processing_s"],
        is_current=bool(row["is_current"]),
        created_at=row["created_at"],
    )


def _candidate(row: sqlite3.Row) -> ClipCandidate:
    components = tuple(
        ScoreComponent(name, label, float(value), float(weight), detail)
        for name, label, value, weight, detail in json.loads(row["components_json"])
    )
    return ClipCandidate(
        id=row["id"],
        analysis_id=row["analysis_id"],
        video_id=row["video_id"],
        key=row["candidate_key"],
        rank=row["rank"],
        start_s=row["start_s"],
        end_s=row["end_s"],
        category=row["category"],
        methods=tuple(json.loads(row["methods_json"])),
        title=romanize(row["title"]),
        excerpt=romanize(row["excerpt"]),
        hook=_span(row["hook_json"]),
        context=_span(row["context_json"]) if row["context_json"] else None,
        payoff=_span(row["payoff_json"]),
        score=row["score"],
        confidence=row["confidence"],
        components=components,
        strengths=tuple(json.loads(row["strengths_json"])),
        weaknesses=tuple(json.loads(row["weaknesses_json"])),
        explanation=romanize(row["explanation"]),
        signal_positions=tuple(json.loads(row["signal_positions_json"])),
        evidence=json.loads(row["evidence_json"]),
        decision=ClipDecision(row["decision"]) if row["decision"] else None,
        adjusted_start_s=row["adjusted_start_s"],
        adjusted_end_s=row["adjusted_end_s"],
        decided_at=row["decided_at"],
    )


@dataclass(frozen=True, slots=True)
class NewAnalysis:
    """Everything needed to store a finished analysis."""

    video_id: int
    transcript_id: int
    analysis_version: int
    scoring_version: int
    params_hash: str
    params: dict[str, Any]
    duration_s: float
    sources: dict[str, Any]
    stats: dict[str, Any]
    signals: Sequence[ContentSignal]
    candidates: Sequence[tuple[str, ScoredCandidate]]
    """Stable key and suggestion, best first (rank is the position, from 1)."""
    processing_s: float | None = None


class ClipRepository:
    """Reads and writes clip analyses, suggestions and decisions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ analyses

    def save(
        self,
        new: NewAnalysis,
        *,
        job_id: int | None = None,
        worker_id: str | None = None,
    ) -> ClipAnalysis:
        """Store an analysis and make it the video's current one.

        An existing analysis of the same video with the same parameter hash is replaced.
        Raises ``JobOwnershipError`` if the job is no longer running on this worker;
        nothing is written then.
        """
        with transaction(self.conn):
            assert_job_owned(self.conn, job_id, worker_id)
            self.conn.execute(
                "DELETE FROM clip_analyses WHERE video_id = ? AND params_hash = ?",
                (new.video_id, new.params_hash),
            )
            self.conn.execute(
                "UPDATE clip_analyses SET is_current = 0 WHERE video_id = ? AND is_current = 1",
                (new.video_id,),
            )
            cursor = self.conn.execute(
                "INSERT INTO clip_analyses (video_id, transcript_id, job_id, analysis_version, "
                "scoring_version, params_hash, params_json, duration_s, sources_json, "
                "stats_json, signal_count, candidate_count, processing_s, is_current, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    new.video_id,
                    new.transcript_id,
                    job_id,
                    new.analysis_version,
                    new.scoring_version,
                    new.params_hash,
                    _dump(new.params),
                    new.duration_s,
                    _dump(new.sources),
                    _dump(new.stats),
                    len(new.signals),
                    len(new.candidates),
                    new.processing_s,
                    utc_now(),
                ),
            )
            analysis_id = cursor.lastrowid
            if analysis_id is None:
                raise RuntimeError("Analysis insert returned no id")
            self.conn.executemany(
                "INSERT INTO clip_signals (analysis_id, position, kind, source, start_s, end_s, "
                "strength, confidence, label, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        analysis_id,
                        position,
                        s.kind.value,
                        s.source.value,
                        s.start_s,
                        s.end_s,
                        s.strength,
                        s.confidence,
                        s.label,
                        _dump(s.evidence),
                    )
                    for position, s in enumerate(new.signals)
                ],
            )
            self.conn.executemany(
                "INSERT INTO clip_candidates (analysis_id, video_id, candidate_key, rank, start_s, "
                "end_s, category, methods_json, title, excerpt, hook_json, context_json, "
                "payoff_json, score, confidence, components_json, strengths_json, "
                "weaknesses_json, explanation, signal_positions_json, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        analysis_id,
                        new.video_id,
                        key,
                        rank,
                        c.start_s,
                        c.end_s,
                        c.category,
                        _dump(list(c.methods)),
                        c.title,
                        c.excerpt,
                        _span_json(c.hook),
                        _span_json(c.context) if c.context else None,
                        _span_json(c.payoff),
                        c.score,
                        c.confidence,
                        _dump(
                            [[x.name, x.label, x.value, x.weight, x.detail] for x in c.components]
                        ),
                        _dump(list(c.strengths)),
                        _dump(list(c.weaknesses)),
                        c.explanation,
                        _dump(list(c.signal_indices)),
                        _dump(c.evidence),
                    )
                    for rank, (key, c) in enumerate(new.candidates, start=1)
                ],
            )
        saved = self.get(analysis_id)
        if saved is None:
            raise RuntimeError("Analysis vanished after insert")
        return saved

    def get(self, analysis_id: int) -> ClipAnalysis | None:
        """One analysis."""
        row = self.conn.execute(
            f"SELECT {_ANALYSIS_COLUMNS} FROM clip_analyses WHERE id = ?",  # noqa: S608
            (analysis_id,),
        ).fetchone()
        return _analysis(row) if row else None

    def current(self, video_id: int) -> ClipAnalysis | None:
        """The analysis the dashboard and later phases should use for a video."""
        row = self.conn.execute(
            f"SELECT {_ANALYSIS_COLUMNS} FROM clip_analyses "  # noqa: S608
            "WHERE video_id = ? AND is_current = 1",
            (video_id,),
        ).fetchone()
        return _analysis(row) if row else None

    def find(self, video_id: int, params_hash: str) -> ClipAnalysis | None:
        """The analysis made from these exact parameters, if any."""
        row = self.conn.execute(
            f"SELECT {_ANALYSIS_COLUMNS} FROM clip_analyses "  # noqa: S608
            "WHERE video_id = ? AND params_hash = ?",
            (video_id, params_hash),
        ).fetchone()
        return _analysis(row) if row else None

    def make_current(
        self, analysis_id: int, *, job_id: int | None = None, worker_id: str | None = None
    ) -> ClipAnalysis:
        """Make an existing analysis the current one for its video (used when reusing)."""
        with transaction(self.conn):
            assert_job_owned(self.conn, job_id, worker_id)
            row = self.conn.execute(
                "SELECT video_id FROM clip_analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise ClipNotFoundError(f"No clip analysis with id {analysis_id}")
            self.conn.execute(
                "UPDATE clip_analyses SET is_current = 0 WHERE video_id = ? AND is_current = 1",
                (row["video_id"],),
            )
            self.conn.execute(
                "UPDATE clip_analyses SET is_current = 1 WHERE id = ?", (analysis_id,)
            )
        analysis = self.get(analysis_id)
        if analysis is None:
            raise RuntimeError("Analysis vanished")
        return analysis

    def current_by_video(self) -> dict[int, ClipAnalysis]:
        """Current analysis of every video that has one."""
        rows = self.conn.execute(
            f"SELECT {_ANALYSIS_COLUMNS} FROM clip_analyses WHERE is_current = 1"  # noqa: S608
        ).fetchall()
        return {r["video_id"]: _analysis(r) for r in rows}

    def signals(self, analysis_id: int) -> list[StoredSignal]:
        """Signals of an analysis in timeline order (their stored position)."""
        rows = self.conn.execute(
            "SELECT id, analysis_id, position, kind, source, start_s, end_s, strength, "
            "confidence, label, evidence_json FROM clip_signals "
            "WHERE analysis_id = ? ORDER BY position",
            (analysis_id,),
        ).fetchall()
        return [
            StoredSignal(
                id=r["id"],
                analysis_id=r["analysis_id"],
                position=r["position"],
                kind=SignalKind(r["kind"]),
                source=SignalSource(r["source"]),
                start_s=r["start_s"],
                end_s=r["end_s"],
                strength=r["strength"],
                confidence=r["confidence"],
                label=r["label"],
                evidence=json.loads(r["evidence_json"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ candidates

    def candidates(self, analysis_id: int) -> list[ClipCandidate]:
        """Suggestions of an analysis, best first, with the user's decisions."""
        rows = self.conn.execute(
            f"SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM} "
            "WHERE c.analysis_id = ? ORDER BY c.rank",
            (analysis_id,),
        ).fetchall()
        return [_candidate(r) for r in rows]

    def candidate(self, candidate_id: int) -> ClipCandidate | None:
        """One suggestion."""
        row = self.conn.execute(
            f"SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM} WHERE c.id = ?",
            (candidate_id,),
        ).fetchone()
        return _candidate(row) if row else None

    def candidate_by_key(self, video_id: int, key: str) -> ClipCandidate | None:
        """The suggestion with this stable key in the video's current analysis."""
        row = self.conn.execute(
            f"SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM} "
            "JOIN clip_analyses a ON a.id = c.analysis_id AND a.is_current = 1 "
            "WHERE c.video_id = ? AND c.candidate_key = ?",
            (video_id, key),
        ).fetchone()
        return _candidate(row) if row else None

    # ------------------------------------------------------------------ decisions

    def decide(self, candidate_id: int, decision: ClipDecision | None) -> ClipCandidate:
        """Favorite, reject or clear a suggestion. Survives re-analysis (stored by key)."""
        candidate = self._require(candidate_id)
        with transaction(self.conn):
            self._upsert(
                candidate,
                decision=decision.value if decision else None,
                adjusted=(None, None),
                set_decision=True,
            )
            self._drop_empty(candidate)
        return self._require(candidate_id)

    def set_adjustment(
        self, candidate_id: int, start_s: float | None, end_s: float | None
    ) -> ClipCandidate:
        """Record adjusted boundaries for later phases; ``None`` for both clears them."""
        candidate = self._require(candidate_id)
        if (start_s is None) != (end_s is None):
            raise ClipAnalysisError("Give both an adjusted start and an adjusted end, or neither")
        if start_s is not None and end_s is not None and not 0 <= start_s < end_s:
            raise ClipAnalysisError("The adjusted clip must start before it ends")
        with transaction(self.conn):
            self._upsert(candidate, decision=None, adjusted=(start_s, end_s), set_decision=False)
            self._drop_empty(candidate)
        return self._require(candidate_id)

    def feedback(self, video_id: int) -> Feedback:
        """Past decisions as ranking feedback: this video's spans, all videos' category bias."""
        rows = self.conn.execute(
            "SELECT video_id, decision, start_s, end_s, category FROM clip_decisions "
            "WHERE decision IS NOT NULL ORDER BY id"
        ).fetchall()
        favorites: list[tuple[float, float]] = []
        rejected: list[tuple[float, float]] = []
        counts: dict[str, tuple[int, int]] = {}
        for r in rows:
            fav, rej = counts.get(r["category"], (0, 0))
            counts[r["category"]] = (
                (fav + 1, rej) if r["decision"] == "favorite" else (fav, rej + 1)
            )
            if r["video_id"] == video_id:
                (favorites if r["decision"] == "favorite" else rejected).append(
                    (r["start_s"], r["end_s"])
                )
        return Feedback(tuple(favorites), tuple(rejected), category_bias(counts))

    def decision_counts(self, video_id: int) -> dict[str, int]:
        """Number of favorited and rejected suggestions of a video."""
        rows = self.conn.execute(
            "SELECT decision, count(*) AS n FROM clip_decisions "
            "WHERE video_id = ? AND decision IS NOT NULL GROUP BY decision",
            (video_id,),
        ).fetchall()
        return {r["decision"]: r["n"] for r in rows}

    def _require(self, candidate_id: int) -> ClipCandidate:
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise ClipNotFoundError(f"No clip suggestion with id {candidate_id}")
        return candidate

    def _upsert(
        self,
        candidate: ClipCandidate,
        *,
        decision: str | None,
        adjusted: tuple[float | None, float | None],
        set_decision: bool,
    ) -> None:
        """Insert the decision row for a suggestion, or update only the chosen part."""
        now = utc_now()
        self.conn.execute(
            "INSERT INTO clip_decisions (video_id, candidate_key, decision, start_s, end_s, "
            "category, adjusted_start_s, adjusted_end_s, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (video_id, candidate_key) DO UPDATE SET "
            "decision = CASE WHEN ? THEN excluded.decision ELSE decision END, "
            "adjusted_start_s = "
            "CASE WHEN ? THEN adjusted_start_s ELSE excluded.adjusted_start_s END, "
            "adjusted_end_s = CASE WHEN ? THEN adjusted_end_s ELSE excluded.adjusted_end_s END, "
            "decided_at = excluded.decided_at",
            (
                candidate.video_id,
                candidate.key,
                decision,
                candidate.start_s,
                candidate.end_s,
                candidate.category,
                adjusted[0],
                adjusted[1],
                now,
                set_decision,
                set_decision,
                set_decision,
            ),
        )

    def _drop_empty(self, candidate: ClipCandidate) -> None:
        self.conn.execute(
            "DELETE FROM clip_decisions WHERE video_id = ? AND candidate_key = ? "
            "AND decision IS NULL AND adjusted_start_s IS NULL",
            (candidate.video_id, candidate.key),
        )
