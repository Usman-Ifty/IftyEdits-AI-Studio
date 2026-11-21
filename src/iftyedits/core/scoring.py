"""Clip scoring and ranking (Phase 2C): deterministic, weighted and explainable.

The score is *algorithmic*: a weighted mean of twelve component values (each 0 to 1,
computed from measured transcript, audio and video evidence), minus named penalties,
scaled to 0-100. No model, no randomness. Identical inputs and weights always give the
identical score, order and explanation. A component that cannot be assessed (for
example wording cues in a non-English transcript) gets weight 0 and is reported as not
assessed instead of being guessed.

The score is a recommendation about how well a moment *stands alone as a short clip*.
It does not predict views, virality, copyright status or policy compliance.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from iftyedits.core.candidates import CandidateConfig, sentences_in
from iftyedits.core.clip_models import (
    CandidateDraft,
    ClipSpan,
    ContentSignal,
    Evidence,
    ScoreComponent,
    ScoredCandidate,
    Sentence,
    SignalKind,
)
from iftyedits.core.signals import (
    STOPWORDS,
    STRONG_CUE,
    Boilerplate,
    boilerplate_match,
    hook_strength,
    is_question,
    starts_with_connector,
    tokens,
)

SCORING_VERSION = 2
"""Bump when components, weights or penalties change, so stored scores can be recomputed."""

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "hook": 0.16,
    "context": 0.10,
    "narrative": 0.10,
    "payoff": 0.12,
    "clarity": 0.07,
    "pacing": 0.07,
    "emotion": 0.07,
    "surprise": 0.06,
    "usefulness": 0.06,
    "duration_fit": 0.08,
    "transcript_quality": 0.06,
    "coherence": 0.05,
}

LABELS: Mapping[str, str] = {
    "hook": "Hook strength",
    "context": "Context completeness",
    "narrative": "Narrative structure",
    "payoff": "Payoff and ending",
    "clarity": "Clarity",
    "pacing": "Pacing",
    "emotion": "Emotional / humorous wording",
    "surprise": "Surprise",
    "usefulness": "Usefulness",
    "duration_fit": "Duration fit",
    "transcript_quality": "Transcript quality",
    "coherence": "Coherence",
}

CATEGORY_LABELS: Mapping[str, str] = {
    "question_answer": "Question and answer",
    "story": "Story",
    "humor": "Funny moment",
    "surprise": "Surprising moment",
    "emotional": "Emotional moment",
    "advice": "Advice or explanation",
    "motivation": "Motivation",
    "dialogue": "Back-and-forth",
    "topic": "Topic segment",
}

_CATEGORY_OF: Mapping[SignalKind, str] = {
    SignalKind.QUESTION_ANSWER: "question_answer",
    SignalKind.STORY: "story",
    SignalKind.HUMOR_CUE: "humor",
    SignalKind.SURPRISE: "surprise",
    SignalKind.EMOTIONAL_WORDING: "emotional",
    SignalKind.INSIGHT: "advice",
    SignalKind.MOTIVATION: "motivation",
    SignalKind.RAPID_EXCHANGE: "dialogue",
}

_WORDING_COMPONENTS = frozenset({"emotion", "surprise", "usefulness"})
PENALTY_CAP = 30.0
BOILERPLATE_FLOOR = 0.08
"""Below this share of boilerplate words a passing mention costs nothing."""
BOILERPLATE_POINTS_PER_SHARE = 45.0
BOILERPLATE_MAX_POINTS = 25.0
_AFFECT_CATEGORIES = frozenset({"humor", "emotional"})
FEEDBACK_MAX_POINTS = 4.0
FAVORITE_BONUS = 8.0
REJECTED_PENALTY = 25.0
SIMILAR_OVERLAP = 0.6


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Component weights; they are normalised, so only the ratios matter."""

    values: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def __post_init__(self) -> None:
        unknown = set(self.values) - set(DEFAULT_WEIGHTS)
        missing = set(DEFAULT_WEIGHTS) - set(self.values)
        if unknown or missing:
            raise ValueError(f"Scoring weights must name exactly {sorted(DEFAULT_WEIGHTS)}")
        if any(v < 0 for v in self.values.values()) or sum(self.values.values()) <= 0:
            raise ValueError("Scoring weights must be non-negative with a positive total")

    def as_dict(self) -> dict[str, float]:
        """Weights in a stable order, for storage and hashing."""
        return {name: self.values[name] for name in DEFAULT_WEIGHTS}


@dataclass(frozen=True, slots=True)
class RankingConfig:
    """How many suggestions to keep and how much they may overlap."""

    max_count: int = 20
    max_overlap: float = 0.5
    """Largest allowed shared fraction of the shorter clip; above it the lower score is dropped."""

    def __post_init__(self) -> None:
        if self.max_count < 1:
            raise ValueError("The number of suggestions must be at least 1")
        if not 0.0 <= self.max_overlap <= 1.0:
            raise ValueError("The allowed overlap must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Feedback:
    """What the user decided before, used to nudge new rankings (never to hide clips)."""

    favorite_spans: tuple[tuple[float, float], ...] = ()
    rejected_spans: tuple[tuple[float, float], ...] = ()
    category_bias: Mapping[str, float] = field(default_factory=dict)
    """Per category, -1 (mostly rejected) to 1 (mostly favorited), across the user's videos."""


def candidate_key(video_sha256: str, start_s: float, end_s: float) -> str:
    """Stable id of a suggestion: the same video and boundaries always give the same key."""
    raw = f"{video_sha256}:{start_s:.2f}:{end_s:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def category_bias(counts: Mapping[str, tuple[int, int]]) -> dict[str, float]:
    """Bias from ``{category: (favorites, rejections)}``; shrunk toward 0 for small counts."""
    return {
        category: round((fav - rej) / (fav + rej + 3), 3)
        for category, (fav, rej) in sorted(counts.items())
    }


def overlap_fraction(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Shared time as a fraction of the shorter span."""
    shared = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return max(0.0, shared) / shorter if shorter > 0 else 0.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _overlapping(
    evidence: Evidence, start_s: float, end_s: float
) -> list[tuple[int, ContentSignal]]:
    return [
        (i, s) for i, s in enumerate(evidence.signals) if s.start_s < end_s and s.end_s > start_s
    ]


def _strongest(signals: Iterable[ContentSignal], *kinds: SignalKind) -> ContentSignal | None:
    found = [s for s in signals if s.kind in kinds]
    return max(found, key=lambda s: (s.strength * s.confidence, -s.start_s)) if found else None


def _mean(values: Sequence[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _span(sentences: Sequence[Sentence]) -> ClipSpan:
    return ClipSpan(
        sentences[0].start_s, sentences[-1].end_s, " ".join(s.text.strip() for s in sentences)
    )


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def _title(first: str) -> str:
    text = re.sub(r"^[\s,;:.\-\u2013\u2014…]+", "", first)
    text = _trim(text.rstrip(" .,;:-"), 70)
    return text[:1].upper() + text[1:] if text else "Untitled clip"


_PRONOUN_OPENERS = frozenset({"it", "that", "this", "they", "he", "she", "them", "those", "these"})


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    dot = sum(a[k] * b[k] for k in a.keys() & b.keys())
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _content_counter(sentences: Sequence[Sentence]) -> Counter[str]:
    return Counter(
        t for s in sentences for t in tokens(s.text) if len(t) > 2 and t not in STOPWORDS
    )


@dataclass(frozen=True, slots=True)
class _Part:
    """One component's measurement: the value plus what was good and what was wrong.

    ``detail`` is the full text shown in the breakdown. ``positive`` is its favourable part
    (listed under Strengths) and ``issues`` its unfavourable notes (listed under Weaknesses),
    so negative evidence never appears as a strength.
    """

    value: float
    detail: str
    positive: str = ""
    issues: tuple[str, ...] = ()
    assessed: bool = True
    absent: bool = False


class _Facts:
    """Measurements about one draft, shared by the components and the explanation."""

    def __init__(self, evidence: Evidence, draft: CandidateDraft, config: CandidateConfig) -> None:
        self.evidence = evidence
        self.draft = draft
        self.config = config
        self.sentences = tuple(sentences_in(evidence, draft))
        self.first = self.sentences[0]
        self.last = self.sentences[-1]
        self.duration = draft.duration_s
        in_window = _overlapping(evidence, draft.start_s, draft.end_s)
        self.indexed = in_window
        self.signals = [s for _, s in in_window]
        self.words = sum(s.word_count for s in self.sentences)
        speech = sum(s.duration_s for s in self.sentences)
        self.rate = self.words / speech if speech > 0 else 0.0
        gaps = [max(0.0, s.gap_after_s - 0.6) for s in self.sentences[:-1]]
        silent = sum(
            min(s.end_s, draft.end_s) - max(s.start_s, draft.start_s)
            for s in self.signals
            if s.kind is SignalKind.SILENCE
        )
        self.dead_s = max(sum(gaps), silent)
        self.dead_ratio = self.dead_s / self.duration if self.duration > 0 else 0.0
        confidences = [s.confidence for s in self.sentences if s.confidence is not None]
        self.mean_confidence = _mean(confidences, 0.6)
        self.known_confidence = bool(confidences)
        self.timed_ratio = _mean([1.0 if s.timed_by_words else 0.0 for s in self.sentences], 0.0)
        self.terminal_ratio = _mean([1.0 if s.terminal else 0.0 for s in self.sentences], 0.0)
        self.topic_shifts = sum(1 for s in self.signals if s.kind is SignalKind.TOPIC_SHIFT)
        self.low_conf = sum(1 for s in self.signals if s.kind is SignalKind.LOW_CONFIDENCE)
        self.boiler: list[Boilerplate | None] = [boilerplate_match(s) for s in self.sentences]
        weighted = sum(
            b.strength * s.word_count
            for b, s in zip(self.boiler, self.sentences, strict=True)
            if b is not None
        )
        self.boiler_share = weighted / self.words if self.words > 0 else 0.0

    def has(self, *kinds: SignalKind) -> bool:
        return any(s.kind in kinds for s in self.signals)

    def strongest(self, *kinds: SignalKind) -> ContentSignal | None:
        return _strongest(self.signals, *kinds)

    def loud_peaks(self) -> int:
        return sum(1 for s in self.signals if s.kind is SignalKind.LOUDNESS_PEAK)


# ------------------------------------------------------------------------- components


def _hook(f: _Facts) -> tuple[_Part, int]:
    """The hook component and the index (within the clip) of the best opening line."""
    lexicon = f.evidence.lexicon_supported
    best: tuple[float, str, int] | None = None
    for position, sentence in enumerate(f.sentences[:3]):
        found = hook_strength(sentence, lexicon)
        if found is None:
            continue
        value = found[0] * (1.0 if position == 0 else 0.85 - 0.1 * position)
        if best is None or value > best[0]:
            best = (value, f"{found[1]}", position)
    if best is not None:
        positive = f"Opening line {best[1]}"
        if best[2] == 0:
            return _Part(min(1.0, best[0]), positive, positive), 0
        issue = f"the hook is sentence {best[2] + 1}, so the clip opens with setup"
        detail = f"{positive} (sentence {best[2] + 1}, so the clip opens with setup)"
        return _Part(min(1.0, best[0]), detail, positive, (issue,)), best[2]
    value = 0.2
    detail = "Opening line has no hook wording"
    positive = ""
    issues: list[str] = []
    if is_question(f.first):
        value, detail = 0.55, "Opens with a question"
        positive = detail
    elif not starts_with_connector(f.first.text):
        value, detail = 0.3, "Opens with a plain statement"
        positive = detail
    if starts_with_connector(f.first.text):
        value *= 0.5
        issues.append("starts with a continuation word")
    if f.first.word_count <= 3:
        value *= 0.7
        issues.append("very short opening")
    if f.boiler[0] is not None:
        value *= 0.5
        issues.append(f"opens on channel boilerplate ({f.boiler[0].label})")
    if issues:
        detail += "; " + "; ".join(issues)
    return _Part(value, detail, positive, tuple(issues)), 0


def _context(f: _Facts) -> _Part:
    first = f.first
    value = 0.0
    good: list[str] = []
    issues: list[str] = []
    if not starts_with_connector(first.text):
        value += 0.4
        good.append("starts on a fresh thought")
    else:
        issues.append("starts with a continuation word")
    if first.index == 0 or first.gap_before_s >= 0.5:
        value += 0.25
        good.append("starts after a pause")
    else:
        issues.append("starts without a pause before it")
    opener = tokens(first.text)[:1]
    if opener and opener[0] not in _PRONOUN_OPENERS:
        value += 0.15
        good.append("opens on a clear subject")
    else:
        issues.append("opens with a pronoun that points back")
    if f.has(SignalKind.QUESTION_ANSWER, SignalKind.STORY, SignalKind.QUESTION):
        value += 0.2
        good.append("sets up a question or story")
    elif f.first.index == 0 or is_question(first):
        value += 0.1
    if f.boiler[0] is not None:
        value *= 0.5
        issues.append(f"starts with channel boilerplate ({f.boiler[0].label})")
    detail = "Self-contained start" if not issues else "Weak start: " + ", ".join(issues)
    positive = "Self-contained start" if not issues else ", ".join(good).capitalize()
    return _Part(_clamp(value), detail, positive, tuple(issues))


def _narrative(f: _Facts) -> _Part:
    count = len(f.sentences)
    if f.has(SignalKind.QUESTION_ANSWER):
        value, detail = 0.9, "Contains a question and its answer"
    elif f.has(SignalKind.STORY) and count >= 4:
        value, detail = 0.8, "Story wording across several sentences"
    elif f.has(SignalKind.STORY):
        value, detail = 0.55, "Story wording but short"
    elif count >= 4:
        value, detail = 0.45, "Several connected sentences, no explicit structure cue"
    else:
        value, detail = 0.25, "Very few sentences"
    if f.has(SignalKind.RAPID_EXCHANGE):
        value = min(1.0, value + 0.1)
        detail += "; lively exchange"
    return _Part(value, detail)


def _payoff(f: _Facts) -> _Part:
    last = f.last
    value = 0.0
    notes: list[str] = []
    good: list[str] = []
    issues: list[str] = []

    def note(text: str, ok: bool) -> None:
        notes.append(text)
        (good if ok else issues).append(text)

    if last.terminal:
        value += 0.35
        note("ends on a full stop", True)
    else:
        note("ends mid-thought", False)
    if last.gap_after_s >= 0.5 or f.draft.last_sentence + 1 >= len(f.evidence.sentences):
        value += 0.2
        note("followed by a pause", True)
    tail = f.sentences[-2:]
    tail_start = tail[0].start_s
    if any(
        s.kind
        in (
            SignalKind.SURPRISE,
            SignalKind.HUMOR_CUE,
            SignalKind.INSIGHT,
            SignalKind.MOTIVATION,
            SignalKind.EMOTIONAL_WORDING,
            SignalKind.LOUDNESS_PEAK,
        )
        and s.end_s > tail_start
        for s in f.signals
    ):
        value += 0.3
        note("a strong moment lands near the end", True)
    if not is_question(last):
        value += 0.15
    else:
        note("ends on an unanswered question", False)
    if f.boiler[-1] is not None:
        value *= 0.5
        note(f"ends on channel boilerplate ({f.boiler[-1].label})", False)
    return _Part(
        _clamp(value),
        ", ".join(notes).capitalize(),
        ", ".join(good).capitalize(),
        tuple(issues),
    )


def _clarity(f: _Facts) -> _Part:
    avg_words = f.words / len(f.sentences)
    if 6 <= avg_words <= 22:
        length_fit = 1.0
    else:
        length_fit = _clamp(
            1.0 - (6 - avg_words) / 6 if avg_words < 6 else 1.0 - (avg_words - 22) / 20
        )
    value = 0.5 * f.terminal_ratio + 0.5 * length_fit
    return _Part(
        value, f"{f.terminal_ratio:.0%} complete sentences, {avg_words:.0f} words per sentence"
    )


def _pacing(f: _Facts) -> _Part:
    if f.rate <= 0:
        return _Part(0.2, "No measurable speech rate")
    if 2.2 <= f.rate <= 3.6:
        rate_fit = 1.0
    elif f.rate < 2.2:
        rate_fit = _clamp(f.rate / 2.2)
    else:
        rate_fit = _clamp(1.0 - (f.rate - 3.6) / 2.4)
    dead_fit = _clamp(1.0 - 3.0 * f.dead_ratio)
    return _Part(
        0.6 * rate_fit + 0.4 * dead_fit, f"{f.rate:.1f} words/s, {f.dead_ratio:.0%} dead time"
    )


def _wording(f: _Facts, *kinds: SignalKind, loud_bonus: float = 0.0) -> _Part:
    """Strength of wording cues (and optionally loudness) with a note."""
    found = f.strongest(*kinds)
    count = sum(1 for s in f.signals if s.kind in kinds)
    value = 0.0
    detail = "No matching cue found"
    if found is not None:
        value = _clamp(found.strength * found.confidence + 0.08 * (count - 1))
        detail = f"{count} cue(s), strongest: {found.label.lower()}"
    if loud_bonus and f.loud_peaks():
        value = _clamp(value + loud_bonus)
        detail += "; volume rise"
    assessed = f.evidence.lexicon_supported or (loud_bonus > 0 and f.evidence.loudness is not None)
    absent = found is None and not (loud_bonus and f.loud_peaks())
    return _Part(value, detail, assessed=assessed, absent=absent)


def _duration_fit(f: _Facts) -> _Part:
    c, d = f.config, f.duration
    if c.min_s <= d <= c.max_s:
        spread = (c.target_s - c.min_s) if d < c.target_s else (c.max_s - c.target_s)
        off = abs(d - c.target_s) / spread if spread > 0 else 0.0
        return _Part(1.0 - 0.3 * off, f"{d:.0f}s is within {c.min_s:.0f}-{c.max_s:.0f}s")
    edge = c.min_s if d < c.min_s else c.max_s
    value = _clamp(0.7 - 1.5 * abs(d - edge) / edge, 0.1, 0.7)
    return _Part(value, f"{d:.0f}s is outside the preferred {c.min_s:.0f}-{c.max_s:.0f}s")


def _quality(f: _Facts) -> _Part:
    low = _clamp(f.low_conf / max(1, len(f.sentences)))
    value = _clamp(f.mean_confidence * (0.6 + 0.4 * f.timed_ratio) * (1.0 - 0.5 * low))
    known = "" if f.known_confidence else " (no confidence data; assumed)"
    return _Part(
        value,
        f"Mean word confidence {f.mean_confidence:.0%}{known}, {f.timed_ratio:.0%} word-timed",
    )


def _coherence(f: _Facts) -> _Part:
    topic = _clamp(1.0 - 0.4 * f.topic_shifts)
    half = len(f.sentences) // 2
    if half >= 2:
        similarity = _cosine(
            _content_counter(f.sentences[:half]), _content_counter(f.sentences[half:])
        )
        value = 0.6 * topic + 0.4 * _clamp(similarity * 2.5)
        return _Part(
            value, f"{f.topic_shifts} topic change(s), halves share {similarity:.0%} vocabulary"
        )
    return _Part(
        0.6 * topic + 0.2, f"{f.topic_shifts} topic change(s); too short to compare halves"
    )


def _components(
    f: _Facts, weights: ScoringWeights
) -> tuple[list[ScoreComponent], int, dict[str, _Part]]:
    hook_part, hook_at = _hook(f)
    wording = f.evidence.lexicon_supported
    raw: dict[str, _Part] = {
        "hook": hook_part,
        "context": _context(f),
        "narrative": _narrative(f),
        "payoff": _payoff(f),
        "clarity": _clarity(f),
        "pacing": _pacing(f),
        "emotion": _wording(f, SignalKind.EMOTIONAL_WORDING, SignalKind.HUMOR_CUE, loud_bonus=0.2),
        "surprise": _wording(f, SignalKind.SURPRISE, loud_bonus=0.25),
        "usefulness": _wording(f, SignalKind.INSIGHT, SignalKind.MOTIVATION),
        "duration_fit": _duration_fit(f),
        "transcript_quality": _quality(f),
        "coherence": _coherence(f),
    }
    out: list[ScoreComponent] = []
    for name in DEFAULT_WEIGHTS:
        part = raw[name]
        weight = weights.values[name]
        detail = part.detail
        if not part.assessed:
            weight = 0.0
            detail = "Not assessed: wording cues need English text" if not wording else detail
        out.append(ScoreComponent(name, LABELS[name], round(part.value, 3), weight, detail))
    return out, hook_at, raw


# --------------------------------------------------------------------------- penalties


def _boilerplate_penalty(f: _Facts) -> tuple[str, float] | None:
    """Penalty for intro, call-to-action and sign-off wording, proportional to its share.

    The share is the fraction of the clip's words that are boilerplate, weighted by cue
    strength, so a clip that is only a "like and subscribe" costs the most while a real
    moment with one incidental mention costs little. Position is not used.
    """
    if f.boiler_share < BOILERPLATE_FLOOR:
        return None
    labels = sorted({b.label for b in f.boiler if b is not None})
    points = min(BOILERPLATE_MAX_POINTS, BOILERPLATE_POINTS_PER_SHARE * f.boiler_share)
    return (
        f"Channel boilerplate: {f.boiler_share:.0%} of the words are {' and '.join(labels)} "
        "wording, not content",
        points,
    )


def _penalties(f: _Facts) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    if starts_with_connector(f.first.text) and f.first.gap_before_s < 0.5 and f.first.index > 0:
        found.append(("Weak opening: starts mid-thought on a continuation word", 6.0))
    if not f.last.terminal:
        found.append(("Incomplete ending: the last sentence has no closing punctuation", 8.0))
    elif f.last.gap_after_s < 0.15 and f.draft.last_sentence + 1 < len(f.evidence.sentences):
        found.append(("Abrupt ending: speech continues right after the clip", 5.0))
    if f.dead_ratio > 0.12:
        found.append(
            (
                f"Excessive silence: {f.dead_ratio:.0%} of the clip is dead time",
                min(10.0, 40.0 * f.dead_ratio),
            )
        )
    if f.mean_confidence < 0.45 and f.known_confidence:
        found.append(("Unreliable transcription: low word confidence", 8.0))
    if f.words < 25:
        found.append(("Very little speech: too thin to stand alone", 8.0))
    if f.topic_shifts:
        found.append(
            (
                f"Mixes topics: {f.topic_shifts} topic change(s) inside",
                min(9.0, 4.5 * f.topic_shifts),
            )
        )
    return found


def _confidence(f: _Facts) -> float:
    """Evidence quality: how much of the analysis rested on measured, reliable data."""
    value = (
        0.45 * f.mean_confidence
        + 0.2 * f.timed_ratio
        + 0.15 * (1.0 if f.evidence.lexicon_supported else 0.0)
        + 0.1 * (1.0 if f.evidence.loudness is not None else 0.0)
        + 0.1 * (1.0 if f.evidence.scene_cuts is not None else 0.0)
    )
    return round(_clamp(value), 3)


_CATEGORY_WEIGHT: Mapping[str, float] = {"question_answer": 0.7}
"""A Q&A pair is one signal while wording cues repeat, so it is discounted when summing."""


def _credible_affect(f: _Facts) -> frozenset[str]:
    """Humor and emotional categories the clip has real evidence for.

    One mild reaction word is not enough to call a whole clip funny or emotional: the clip
    needs a strong cue (laughter marked, "oh my god", "hilarious") or two separate cues.
    """
    strong: set[str] = set()
    count: Counter[str] = Counter()
    for s in f.signals:
        name = _CATEGORY_OF.get(s.kind)
        if name in _AFFECT_CATEGORIES:
            count[name] += 1
            if float(s.evidence.get("cue_weight", 0.0)) >= STRONG_CUE:
                strong.add(name)
    return frozenset(strong | {name for name, n in count.items() if n >= 2})


def _category(f: _Facts) -> str:
    """Dominant moment type: the signal kind with the largest summed strength x confidence."""
    credible = _credible_affect(f)
    totals: dict[str, float] = {}
    for s in f.signals:
        name = _CATEGORY_OF.get(s.kind)
        if name in _AFFECT_CATEGORIES and name not in credible:
            continue
        if name is not None:
            totals[name] = totals.get(name, 0.0) + s.strength * s.confidence * _CATEGORY_WEIGHT.get(
                name, 1.0
            )
    if not totals:
        return "topic"
    order = {name: i for i, name in enumerate(_CATEGORY_OF.values())}
    return min(totals, key=lambda name: (-round(totals[name], 6), order[name]))


def _explain(
    category: str, score: float, strengths: Sequence[str], weaknesses: Sequence[str], conf: float
) -> str:
    text = (
        f"{CATEGORY_LABELS[category]} scoring {score:.0f}/100 "
        f"(algorithmic, evidence quality {conf:.0%})."
    )
    if strengths:
        text += " Strengths: " + "; ".join(strengths[:3]) + "."
    if weaknesses:
        text += " Weaknesses: " + "; ".join(weaknesses[:3]) + "."
    return text


def score_draft(
    evidence: Evidence,
    draft: CandidateDraft,
    config: CandidateConfig,
    weights: ScoringWeights | None = None,
    feedback: Feedback | None = None,
) -> ScoredCandidate:
    """Score one draft. Deterministic: same evidence, draft, config and weights, same result."""
    weights = weights or ScoringWeights()
    facts = _Facts(evidence, draft, config)
    components, hook_at, parts = _components(facts, weights)
    total_weight = sum(c.weight for c in components)
    base = 100.0 * sum(c.value * c.weight for c in components) / total_weight
    boilerplate = _boilerplate_penalty(facts)
    penalties = ([boilerplate] if boilerplate else []) + _penalties(facts)
    penalty_points = min(PENALTY_CAP, sum(points for _, points in penalties))
    category = _category(facts)
    strengths: list[str] = []
    weaknesses = [reason for reason, _ in penalties]
    for c in components:
        if c.weight <= 0:
            continue
        part = parts[c.name]
        if c.value >= 0.7:
            text = part.positive or ("" if part.issues else part.detail)
            if text:
                strengths.append(f"{c.label}: {text}")
        if part.absent:
            continue  # no cue is the absence of a plus, not a defect
        if c.value < 0.4:
            weaknesses.append(f"{c.label}: {part.detail}")
        else:
            weaknesses.extend(f"{c.label}: {issue}" for issue in part.issues)

    adjustment = 0.0
    span = (draft.start_s, draft.end_s)
    if feedback is not None:
        if any(overlap_fraction(span, r) >= SIMILAR_OVERLAP for r in feedback.rejected_spans):
            adjustment -= REJECTED_PENALTY
            weaknesses.append("Overlaps a clip you rejected earlier")
        elif any(overlap_fraction(span, r) >= SIMILAR_OVERLAP for r in feedback.favorite_spans):
            adjustment += FAVORITE_BONUS
            strengths.append("Overlaps a clip you favorited earlier")
        bias = feedback.category_bias.get(category, 0.0)
        if bias:
            adjustment += FEEDBACK_MAX_POINTS * _clamp(bias, -1.0, 1.0)
    score = round(_clamp(base - penalty_points + adjustment, 0.0, 100.0), 2)

    conf = _confidence(facts)
    sentences = facts.sentences
    hook_sentence = sentences[hook_at]
    context = _span(sentences[:hook_at]) if hook_at > 0 else None
    payoff_sentences = (
        sentences[-2:] if sentences[-1].word_count < 4 and len(sentences) > 1 else sentences[-1:]
    )
    signals_in = [i for i, s in facts.indexed]
    cuts = [c for c in (evidence.scene_cuts or ()) if draft.start_s <= c.time_s <= draft.end_s]
    return ScoredCandidate(
        start_s=draft.start_s,
        end_s=draft.end_s,
        category=category,
        methods=draft.methods,
        title=_title(hook_sentence.text),
        excerpt=_trim(" ".join(s.text.strip() for s in sentences), 600),
        hook=ClipSpan(hook_sentence.start_s, hook_sentence.end_s, hook_sentence.text.strip()),
        context=context,
        payoff=_span(payoff_sentences),
        score=score,
        confidence=conf,
        components=tuple(components),
        strengths=tuple(strengths),
        weaknesses=tuple(weaknesses),
        explanation=_explain(category, score, strengths, weaknesses, conf),
        signal_indices=tuple(signals_in),
        evidence={
            "sentences": [draft.first_sentence, draft.last_sentence],
            "words": facts.words,
            "speech_rate_wps": round(facts.rate, 2),
            "dead_time_s": round(facts.dead_s, 2),
            "mean_confidence": round(facts.mean_confidence, 3),
            "scene_cuts": len(cuts),
            "loudness_peaks": facts.loud_peaks(),
            "base_score": round(base, 2),
            "penalties": [{"reason": r, "points": round(p, 2)} for r, p in penalties],
            "boilerplate": {
                "share": round(facts.boiler_share, 3),
                "points": round(boilerplate[1], 2) if boilerplate else 0.0,
                "sentences": [
                    {
                        "sentence": s.index,
                        "kind": b.kind,
                        "phrases": list(b.phrases),
                        "text": _trim(s.text.strip(), 120),
                    }
                    for b, s in zip(facts.boiler, facts.sentences, strict=True)
                    if b is not None
                ],
            },
            "penalty_points": round(penalty_points, 2),
            "feedback_points": round(adjustment, 2),
            "scoring_version": SCORING_VERSION,
        },
    )


def score_candidates(
    evidence: Evidence,
    drafts: Sequence[CandidateDraft],
    config: CandidateConfig,
    weights: ScoringWeights | None = None,
    feedback: Feedback | None = None,
) -> list[ScoredCandidate]:
    """Score every draft that covers at least one sentence."""
    return [
        score_draft(evidence, d, config, weights, feedback)
        for d in drafts
        if d.first_sentence <= d.last_sentence and d.end_s > d.start_s
    ]


def rank_candidates(
    scored: Sequence[ScoredCandidate], config: RankingConfig | None = None
) -> tuple[list[ScoredCandidate], int]:
    """Order by score and drop near-duplicates; returns the kept list and how many dropped.

    Order: score, then evidence quality, then earlier start, then shorter. The greedy walk
    keeps a clip only if it overlaps no better, already kept clip by more than
    ``max_overlap`` of the shorter one, so the list is varied and reproducible.
    """
    config = config or RankingConfig()
    ordered = sorted(scored, key=lambda c: (-c.score, -c.confidence, c.start_s, c.end_s))
    kept: list[ScoredCandidate] = []
    dropped = 0
    for candidate in ordered:
        span = (candidate.start_s, candidate.end_s)
        if any(overlap_fraction(span, (k.start_s, k.end_s)) > config.max_overlap for k in kept):
            dropped += 1
            continue
        if len(kept) >= config.max_count:
            dropped += 1
            continue
        kept.append(candidate)
    return kept, dropped


def evidence_summary(scored: Sequence[ScoredCandidate]) -> dict[str, Any]:
    """Counts per category and the score range, for analysis statistics."""
    counts = Counter(c.category for c in scored)
    return {
        "by_category": dict(sorted(counts.items())),
        "best_score": max((c.score for c in scored), default=None),
        "worst_score": min((c.score for c in scored), default=None),
    }
