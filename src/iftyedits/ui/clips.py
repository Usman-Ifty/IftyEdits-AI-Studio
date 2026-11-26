"""Display helpers for clip suggestions: rows, filters, sorting and explanations.

Pure functions only: no I/O, no Streamlit. The dashboard and the CLI share them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from iftyedits.core.clip_models import ClipAnalysis, ClipCandidate, ClipDecision, StoredSignal
from iftyedits.core.scoring import CATEGORY_LABELS
from iftyedits.ui.formatting import timestamp

SORT_OPTIONS: dict[str, str] = {
    "rank": "Best first (rank)",
    "score": "Score",
    "confidence": "Evidence quality",
    "duration": "Duration (longest first)",
    "start": "Position in video",
}
DECISION_FILTERS = ("all", "undecided", "favorites", "rejected", "not rejected")
DECISION_LABELS = {ClipDecision.FAVORITE: "★ favorite", ClipDecision.REJECTED: "✖ rejected"}


def category_label(category: str) -> str:
    """Readable name of a moment type."""
    return CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize())


def decision_label(decision: ClipDecision | None) -> str:
    """Readable state of a suggestion."""
    return DECISION_LABELS.get(decision, "") if decision else ""


def score_band(score: float) -> str:
    """Plain-words band for a score (a recommendation, not a prediction)."""
    if score >= 75:
        return "strong"
    if score >= 55:
        return "promising"
    if score >= 35:
        return "mixed"
    return "weak"


def confidence_band(confidence: float) -> str:
    """Plain-words band for evidence quality."""
    if confidence >= 0.75:
        return "solid evidence"
    if confidence >= 0.5:
        return "fair evidence"
    return "thin evidence"


def span_label(candidate: ClipCandidate) -> str:
    """``start - end (length s)`` using the user's adjustment when there is one."""
    start, end = candidate.effective_start_s, candidate.effective_end_s
    return f"{timestamp(start)} - {timestamp(end)} ({end - start:.0f}s)"


def card_reason(candidate: ClipCandidate, limit: int = 150) -> str:
    """One plain sentence on why this moment was suggested (cut at a word, never mid-word)."""
    if candidate.strengths:
        text = "Why: " + "; ".join(candidate.strengths[:2]).rstrip(".") + "."
        if len(text) <= limit:
            return text
    text = " ".join(candidate.explanation.split())
    for end in (". ", "! ", "? "):
        if end in text:
            text = text[: text.index(end) + 1]
            break
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"


def card_warning(candidate: ClipCandidate) -> str | None:
    """The most important weakness to show on a card, or ``None``."""
    return candidate.weaknesses[0] if candidate.weaknesses else None


def clip_row(candidate: ClipCandidate) -> dict[str, Any]:
    """One table row for a suggestion."""
    return {
        "Rank": candidate.rank,
        "Score": round(candidate.score),
        "Type": category_label(candidate.category),
        "Start": timestamp(candidate.effective_start_s),
        "End": timestamp(candidate.effective_end_s),
        "Length (s)": round(candidate.effective_end_s - candidate.effective_start_s),
        "Evidence": confidence_band(candidate.confidence),
        "Title": candidate.title,
        "Status": decision_label(candidate.decision),
    }


@dataclass(frozen=True, slots=True)
class ClipFilter:
    """What the list shows."""

    categories: frozenset[str] = frozenset()
    """Empty means every moment type."""
    min_score: float = 0.0
    min_duration_s: float = 0.0
    max_duration_s: float | None = None
    decision: str = "all"


def filter_candidates(candidates: Iterable[ClipCandidate], spec: ClipFilter) -> list[ClipCandidate]:
    """Suggestions matching the filter, in their given order."""
    if spec.decision not in DECISION_FILTERS:
        raise ValueError(f"Unknown decision filter {spec.decision!r}")
    kept: list[ClipCandidate] = []
    for c in candidates:
        duration = c.effective_end_s - c.effective_start_s
        if spec.categories and c.category not in spec.categories:
            continue
        if c.score < spec.min_score or duration < spec.min_duration_s:
            continue
        if spec.max_duration_s is not None and duration > spec.max_duration_s:
            continue
        if not _decision_matches(spec.decision, c.decision):
            continue
        kept.append(c)
    return kept


def _decision_matches(wanted: str, decision: ClipDecision | None) -> bool:
    if wanted == "all":
        return True
    if wanted == "undecided":
        return decision is None
    if wanted == "favorites":
        return decision is ClipDecision.FAVORITE
    if wanted == "rejected":
        return decision is ClipDecision.REJECTED
    return decision is not ClipDecision.REJECTED  # "not rejected"


def sort_candidates(candidates: Sequence[ClipCandidate], by: str) -> list[ClipCandidate]:
    """Order suggestions. Ties fall back to rank, so the order is stable."""
    if by not in SORT_OPTIONS:
        raise ValueError(f"Unknown sort {by!r}")
    keys = {
        "rank": lambda c: (c.rank,),
        "score": lambda c: (-c.score, c.rank),
        "confidence": lambda c: (-c.confidence, c.rank),
        "duration": lambda c: (-(c.effective_end_s - c.effective_start_s), c.rank),
        "start": lambda c: (c.effective_start_s, c.rank),
    }
    return sorted(candidates, key=keys[by])


def component_rows(candidate: ClipCandidate) -> list[dict[str, Any]]:
    """Score breakdown rows (components that were not assessed show weight 0)."""
    return [
        {
            "Factor": c.label,
            "Value": round(c.value * 100),
            "Weight": round(c.weight, 3),
            "Why": c.detail,
        }
        for c in candidate.components
    ]


def signal_row(signal: StoredSignal) -> dict[str, Any]:
    """One evidence row for a signal."""
    return {
        "Source": signal.source.value,
        "Signal": signal.kind.value.replace("_", " "),
        "Start": timestamp(signal.start_s),
        "End": timestamp(signal.end_s),
        "Strength": round(signal.strength, 2),
        "Reliability": round(signal.confidence, 2),
        "Detail": signal.label,
    }


def signals_for(candidate: ClipCandidate, signals: Sequence[StoredSignal]) -> list[StoredSignal]:
    """Signals that fall inside the clip, plus those it was built from."""
    built_from = set(candidate.signal_positions)
    return [
        s
        for s in signals
        if s.position in built_from or (s.end_s > candidate.start_s and s.start_s < candidate.end_s)
    ]


def analysis_facts(analysis: ClipAnalysis) -> dict[str, str]:
    """Facts about a stored analysis for a details panel."""
    sources = analysis.sources
    facts = {
        "Suggestions": str(analysis.candidate_count),
        "Signals found": str(analysis.signal_count),
        "Scoring": f"algorithmic, version {analysis.scoring_version}",
        "Analysed": analysis.created_at[:19].replace("T", " ") + " UTC",
    }
    if analysis.processing_s is not None:
        facts["Took"] = f"{analysis.processing_s:.1f} s"
    for key, label in (("audio", "Audio loudness"), ("video", "Scene changes")):
        part = sources.get(key)
        if isinstance(part, dict):
            facts[label] = "used" if part.get("used") else str(part.get("note", "not used"))
    facts["Faces and speakers"] = "not analysed for suggestions"
    return facts
