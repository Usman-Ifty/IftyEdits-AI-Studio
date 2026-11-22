"""Advisory content review of one Short: categories, flags, risk levels and parsing.

The review reads the transcript only. It cannot see the picture, hear tone or music, or know
what YouTube's systems will decide, so a result is advice for a human, never clearance.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from iftyedits.core.errors import LlmFormatError
from iftyedits.core.metadata import clean_text, extract_json

CATEGORIES: dict[str, str] = {
    "harassment": "Harassment, bullying or humiliation",
    "hate": "Hate speech or slurs",
    "threats": "Threats or violent language",
    "graphic_violence": "Graphic violence",
    "dangerous_acts": "Dangerous acts or challenges",
    "self_harm": "Self-harm references",
    "sexual": "Sexual or explicit content",
    "minors": "Minors (possible exploitation or privacy concern)",
    "misinformation": "Misleading claims or misinformation",
    "scams": "Scams, fraud or harmful instructions",
    "drugs": "Drug-related content",
    "sensitive_events": "Sensitive events",
    "privacy": "Personal information disclosed",
    "deceptive_editing": "Missing context or possibly deceptive editing",
    "other": "Other platform-policy concern",
}
SEVERITIES = ("low", "medium", "high")
SEVERITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}

# Certainty of one flag.
EVIDENCE = "evidence"  # the quoted words really are in the transcript
INFERRED = "inferred"  # the model's judgement; no matching words found

LOW = "low"
REVIEW = "review"
HIGH = "high"
UNABLE = "unable"
RISK_LABELS = {
    LOW: "Low detected risk",
    REVIEW: "Review recommended",
    HIGH: "High risk, manual review required",
    UNABLE: "Unable to assess",
}
RISK_TONES = {LOW: "good", REVIEW: "warn", HIGH: "bad", UNABLE: "muted"}

# Review record status.
STATUS_OK = "ok"  # a model reviewed the transcript
STATUS_MANUAL = "manual"  # the user recorded a manual review
STATUS_UNAVAILABLE = "unavailable"  # no model configured
STATUS_FAILED = "failed"  # the model call or answer failed

DISCLAIMER = (
    "Advisory only. This review reads the spoken words (transcript), not the picture, music or "
    "tone, and it does not check every YouTube policy. A result of “low detected risk” is not "
    "a guarantee that a video complies with YouTube's rules, and you remain responsible for it."
)

RESOLUTION_DISMISSED = "dismissed"
RESOLUTION_ACKNOWLEDGED = "acknowledged"
RESOLUTION_LABELS = {
    RESOLUTION_DISMISSED: "Dismissed as a false positive",
    RESOLUTION_ACKNOWLEDGED: "Acknowledged, still unresolved",
}


@dataclass(frozen=True, slots=True)
class SafetyFlag:
    """One concern raised for a stretch of the clip."""

    key: str
    category: str
    severity: str
    certainty: str
    start_s: float | None
    end_s: float | None
    excerpt: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "key": self.key,
            "category": self.category,
            "severity": self.severity,
            "certainty": self.certainty,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "excerpt": self.excerpt,
            "explanation": self.explanation,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SafetyFlag:
        """Rebuild from stored JSON."""

        def _num(value: object) -> float | None:
            return float(value) if isinstance(value, int | float) else None

        return cls(
            key=str(raw.get("key", "")),
            category=str(raw.get("category", "other")),
            severity=str(raw.get("severity", "medium")),
            certainty=str(raw.get("certainty", INFERRED)),
            start_s=_num(raw.get("start_s")),
            end_s=_num(raw.get("end_s")),
            excerpt=str(raw.get("excerpt", "")),
            explanation=str(raw.get("explanation", "")),
        )

    @property
    def label(self) -> str:
        """Readable category name."""
        return CATEGORIES.get(self.category, CATEGORIES["other"])


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", folded).strip()


def excerpt_in_transcript(excerpt: str, transcript: str) -> bool:
    """True when the quoted words appear in the transcript (ignoring case and punctuation)."""
    wanted = _norm(excerpt)
    return bool(wanted) and wanted in _norm(transcript)


def flag_key(category: str, start_s: float | None, excerpt: str) -> str:
    """Stable id for a flag, so a dismissal survives a re-review that finds the same concern."""
    seed = f"{category}|{round(start_s or 0.0, 0)}|{_norm(excerpt)[:80]}"
    return hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:12]


def risk_level(flags: Iterable[SafetyFlag]) -> str:
    """Overall level from the flags, by fixed rules (the model never sets it)."""
    items = list(flags)
    if not items:
        return LOW
    if any(
        f.severity == "high" or (f.severity == "medium" and f.certainty == EVIDENCE) for f in items
    ):
        return HIGH
    return REVIEW


def parse_safety(
    text: str, transcript: str, *, clip_duration_s: float
) -> tuple[tuple[SafetyFlag, ...], str, list[str]]:
    """Validate a model answer. Returns ``(flags, summary, notes)``.

    A flag whose quoted words are not in the transcript is kept but marked *inferred*, and its
    severity is capped at medium: the model may not claim "high" without evidence we can check.
    """
    data = extract_json(text)
    raw_flags = data.get("flags")
    if not isinstance(raw_flags, list):
        raise LlmFormatError("The review answer had no list of flags.")
    flags: list[SafetyFlag] = []
    notes: list[str] = []
    seen: set[str] = set()
    for item in raw_flags[:30]:
        if not isinstance(item, Mapping):
            continue
        category = clean_text(item.get("category"), 30).casefold()
        if category not in CATEGORIES:
            category = "other"
        severity = clean_text(item.get("severity"), 10).casefold()
        if severity not in SEVERITIES:
            severity = "medium"
        excerpt = clean_text(item.get("excerpt"), 240)
        explanation = clean_text(item.get("explanation"), 400)
        if not explanation and not excerpt:
            continue
        start = _time(item.get("start_s"), clip_duration_s)
        end = _time(item.get("end_s"), clip_duration_s)
        if start is not None and end is not None and end < start:
            start, end = end, start
        verified = excerpt_in_transcript(excerpt, transcript)
        certainty = EVIDENCE if verified else INFERRED
        if not verified and severity == "high":
            severity = "medium"
            notes.append("A high-severity flag without matching transcript words was lowered.")
        key = flag_key(category, start, excerpt or explanation)
        if key in seen:
            continue
        seen.add(key)
        flags.append(
            SafetyFlag(key, category, severity, certainty, start, end, excerpt, explanation)
        )
    summary = clean_text(data.get("summary"), 500)
    return tuple(flags), summary, notes


def _time(value: object, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(min(max(float(value), 0.0), limit), 2)
