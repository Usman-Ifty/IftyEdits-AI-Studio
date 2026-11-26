"""Display helpers for clip suggestions: rows, filters, sorting, signals, job labels."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from iftyedits.core.clip_models import ClipCandidate, ClipDecision
from iftyedits.core.models import Job, JobStatus, Video
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
from iftyedits.ui.clips import (
    ClipFilter,
    analysis_facts,
    category_label,
    clip_row,
    component_rows,
    confidence_band,
    decision_label,
    filter_candidates,
    score_band,
    signal_row,
    signals_for,
    sort_candidates,
    span_label,
)
from iftyedits.ui.formatting import job_outcome, job_row
from tests.clip_db_fakes import save_transcript
from tests.clip_fakes import new_analysis, podcast
from tests.conftest import MakeMedia


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Show.mp4")).video


@pytest.fixture
def candidates(db_conn: sqlite3.Connection, video: Video) -> list[ClipCandidate]:
    repo = ClipRepository(db_conn)
    tid = save_transcript(db_conn, video, podcast())
    saved = repo.save(new_analysis(video.sha256, video.id, tid, count=6))
    return repo.candidates(saved.id)


def test_rows_and_labels(candidates: list[ClipCandidate]) -> None:
    row = clip_row(candidates[0])
    assert row["Rank"] == 1 and 0 <= row["Score"] <= 100
    assert row["Type"] == category_label(candidates[0].category)
    assert row["Status"] == ""
    assert category_label("made_up_type") == "Made up type"
    assert decision_label(ClipDecision.FAVORITE) == "★ favorite" and decision_label(None) == ""
    assert (score_band(80), score_band(60), score_band(40), score_band(10)) == (
        "strong",
        "promising",
        "mixed",
        "weak",
    )
    assert confidence_band(0.9) == "solid evidence" and confidence_band(0.1) == "thin evidence"
    assert "s)" in span_label(candidates[0])


def test_adjusted_boundaries_show_in_the_row(candidates: list[ClipCandidate]) -> None:
    c = replace(candidates[0], adjusted_start_s=10.0, adjusted_end_s=30.0)
    assert clip_row(c)["Length (s)"] == 20 and span_label(c).endswith("(20s)")


def test_sorting_is_stable_and_complete(candidates: list[ClipCandidate]) -> None:
    by_score = sort_candidates(candidates, "score")
    assert [c.score for c in by_score] == sorted((c.score for c in candidates), reverse=True)
    assert [c.start_s for c in sort_candidates(candidates, "start")] == sorted(
        c.start_s for c in candidates
    )
    longest = sort_candidates(candidates, "duration")
    assert longest[0].duration_s == max(c.duration_s for c in candidates)
    assert [c.rank for c in sort_candidates(candidates, "rank")] == sorted(
        c.rank for c in candidates
    )
    assert len(sort_candidates(candidates, "confidence")) == len(candidates)
    with pytest.raises(ValueError, match="Unknown sort"):
        sort_candidates(candidates, "nope")


def test_filters(candidates: list[ClipCandidate]) -> None:
    assert filter_candidates(candidates, ClipFilter()) == candidates
    kind = candidates[0].category
    only = filter_candidates(candidates, ClipFilter(categories=frozenset({kind})))
    assert only and all(c.category == kind for c in only)
    top = max(c.score for c in candidates)
    assert all(c.score >= top for c in filter_candidates(candidates, ClipFilter(min_score=top)))
    assert filter_candidates(candidates, ClipFilter(min_score=101)) == []
    shortest = min(c.duration_s for c in candidates)
    short = filter_candidates(candidates, ClipFilter(max_duration_s=shortest))
    assert short and all(c.duration_s <= shortest for c in short)
    assert filter_candidates(candidates, ClipFilter(min_duration_s=10_000)) == []

    marked = [
        replace(candidates[0], decision=ClipDecision.FAVORITE),
        replace(candidates[1], decision=ClipDecision.REJECTED),
        candidates[2],
    ]
    assert filter_candidates(marked, ClipFilter(decision="favorites")) == [marked[0]]
    assert filter_candidates(marked, ClipFilter(decision="rejected")) == [marked[1]]
    assert filter_candidates(marked, ClipFilter(decision="undecided")) == [marked[2]]
    assert filter_candidates(marked, ClipFilter(decision="not rejected")) == [marked[0], marked[2]]
    with pytest.raises(ValueError, match="decision filter"):
        filter_candidates(marked, ClipFilter(decision="bogus"))


def test_breakdown_and_signal_evidence(
    db_conn: sqlite3.Connection, candidates: list[ClipCandidate]
) -> None:
    rows = component_rows(candidates[0])
    assert len(rows) == 13 and all(0 <= r["Value"] <= 100 and r["Why"] for r in rows)
    repo = ClipRepository(db_conn)
    signals = repo.signals(candidates[0].analysis_id)
    inside = signals_for(candidates[0], signals)
    assert inside and all(
        s.position in candidates[0].signal_positions
        or (s.end_s > candidates[0].start_s and s.start_s < candidates[0].end_s)
        for s in inside
    )
    row = signal_row(inside[0])
    assert row["Source"] in {"transcript", "audio", "video"} and row["Detail"]


def test_analysis_facts_are_honest(
    db_conn: sqlite3.Connection, candidates: list[ClipCandidate]
) -> None:
    analysis = ClipRepository(db_conn).get(candidates[0].analysis_id)
    assert analysis is not None
    facts = analysis_facts(analysis)
    assert facts["Scoring"].startswith("algorithmic")
    assert "not analysed" in facts["Faces and speakers"]


def _job(result: dict[str, object] | None) -> Job:
    return Job(
        id=1,
        kind="analyze_clips",
        payload={"video_id": 3},
        status=JobStatus.SUCCEEDED,
        progress=1.0,
        message=None,
        error=None,
        result=result,
        attempts=1,
        created_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        finished_at=None,
        heartbeat_at=None,
        worker_id=None,
        cancel_requested_at=None,
    )


def test_job_labels() -> None:
    assert job_outcome(_job({"analysis_id": 1, "candidates": 4, "reused": False})) == (
        "4 suggestions"
    )
    assert job_outcome(_job({"analysis_id": 1, "candidates": 4, "reused": True})) == "reused"
    row = job_row(_job(None))
    assert row["Kind"] == "analyse clips" and row["File"] == "video #3"


def test_card_text_prefers_strengths_and_stays_short(candidates: list[ClipCandidate]) -> None:
    from iftyedits.ui.clips import card_reason, card_warning

    base = candidates[0]
    rich = replace(base, strengths=("Strong opening hook", "Clear payoff"), weaknesses=("Thin",))
    assert card_reason(rich) == "Why: Strong opening hook; Clear payoff."
    assert card_warning(rich) == "Thin"
    bare = replace(base, strengths=(), weaknesses=(), explanation="A. " + "word " * 80)
    assert card_reason(bare) == "A." and card_warning(bare) is None
    long = replace(base, strengths=(), explanation="word " * 80)
    assert len(card_reason(long)) <= 150 and card_reason(long).endswith("\u2026")
