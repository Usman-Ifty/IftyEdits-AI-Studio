"""Domain models for clip intelligence (Phase 2): signals, candidates and scores.

Plain immutable dataclasses with no I/O. Every signal records where its evidence came
from (``SignalSource``): the transcript's wording and timings, the prepared audio's
loudness, or frame differences of the proxy video. Nothing here claims to recognise
emotions, faces or speakers; labels describe what was measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SignalSource(StrEnum):
    """Where a signal's evidence comes from."""

    TRANSCRIPT = "transcript"
    """Wording, punctuation and timings of the current transcript."""
    AUDIO = "audio"
    """Loudness of the prepared 16 kHz audio."""
    VIDEO = "video"
    """Frame differences of the prepared proxy video."""


class SignalKind(StrEnum):
    """What a signal measured."""

    QUESTION = "question"
    QUESTION_ANSWER = "question_answer"
    HOOK = "hook"
    STORY = "story"
    EMOTIONAL_WORDING = "emotional_wording"
    HUMOR_CUE = "humor_cue"
    SURPRISE = "surprise"
    INSIGHT = "insight"
    MOTIVATION = "motivation"
    RAPID_EXCHANGE = "rapid_exchange"
    TOPIC_SHIFT = "topic_shift"
    FAST_PACE = "fast_pace"
    PAUSE = "pause"
    LOW_CONFIDENCE = "low_confidence"
    SILENCE = "silence"
    LOUDNESS_PEAK = "loudness_peak"
    SCENE_CUT = "scene_cut"


SIGNAL_SOURCES: dict[SignalKind, SignalSource] = {
    **{
        kind: SignalSource.TRANSCRIPT
        for kind in (
            SignalKind.QUESTION,
            SignalKind.QUESTION_ANSWER,
            SignalKind.HOOK,
            SignalKind.STORY,
            SignalKind.EMOTIONAL_WORDING,
            SignalKind.HUMOR_CUE,
            SignalKind.SURPRISE,
            SignalKind.INSIGHT,
            SignalKind.MOTIVATION,
            SignalKind.RAPID_EXCHANGE,
            SignalKind.TOPIC_SHIFT,
            SignalKind.FAST_PACE,
            SignalKind.PAUSE,
            SignalKind.LOW_CONFIDENCE,
        )
    },
    SignalKind.SILENCE: SignalSource.AUDIO,
    SignalKind.LOUDNESS_PEAK: SignalSource.AUDIO,
    SignalKind.SCENE_CUT: SignalSource.VIDEO,
}

SIGNAL_LABELS: dict[SignalKind, str] = {
    SignalKind.QUESTION: "Question",
    SignalKind.QUESTION_ANSWER: "Question and answer",
    SignalKind.HOOK: "Hook-style opening line",
    SignalKind.STORY: "Story wording",
    SignalKind.EMOTIONAL_WORDING: "Emotional wording",
    SignalKind.HUMOR_CUE: "Humor cue in the transcript",
    SignalKind.SURPRISE: "Surprise or reveal wording",
    SignalKind.INSIGHT: "Explanation or advice wording",
    SignalKind.MOTIVATION: "Motivational wording",
    SignalKind.RAPID_EXCHANGE: "Rapid back-and-forth",
    SignalKind.TOPIC_SHIFT: "Topic change",
    SignalKind.FAST_PACE: "Fast speech",
    SignalKind.PAUSE: "Pause in speech",
    SignalKind.LOW_CONFIDENCE: "Uncertain transcription",
    SignalKind.SILENCE: "Silence",
    SignalKind.LOUDNESS_PEAK: "Sudden volume rise",
    SignalKind.SCENE_CUT: "Scene change",
}


@dataclass(frozen=True, slots=True)
class Sentence:
    """A sentence (or sentence-like unit) rebuilt from transcript words or segments."""

    index: int
    start_s: float
    end_s: float
    text: str
    word_count: int
    terminal: bool
    """True if the text ends with sentence punctuation (. ? ! …)."""
    confidence: float | None
    """Mean word probability (or a value derived from the segment log probability)."""
    timed_by_words: bool
    """False when timings were interpolated inside a segment without word timings."""
    gap_before_s: float = 0.0
    gap_after_s: float = 0.0

    @property
    def duration_s(self) -> float:
        """Length in seconds."""
        return self.end_s - self.start_s


@dataclass(frozen=True, slots=True)
class ContentSignal:
    """One piece of evidence on the timeline."""

    kind: SignalKind
    source: SignalSource
    start_s: float
    end_s: float
    strength: float
    """How pronounced the measured feature is, 0 to 1."""
    confidence: float
    """How reliable the underlying data is, 0 to 1."""
    label: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoudnessEnvelope:
    """RMS loudness in dBFS per frame of the prepared audio."""

    frame_s: float
    db: tuple[float, ...]

    @property
    def duration_s(self) -> float:
        """Covered length in seconds."""
        return self.frame_s * len(self.db)

    def window(self, start_s: float, end_s: float) -> tuple[float, ...]:
        """Frames overlapping ``[start_s, end_s)``."""
        if not self.db or end_s <= start_s or self.frame_s <= 0:
            return ()
        first = max(0, int(start_s / self.frame_s))
        last = min(len(self.db), int(end_s / self.frame_s) + 1)
        return self.db[first:last]


@dataclass(frozen=True, slots=True)
class SceneCut:
    """A detected scene change in the proxy video."""

    time_s: float
    score: float
    """FFmpeg scene score (0 to 1): how different the frame is from the previous one."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the analysis measured for one video, on a common timeline."""

    duration_s: float
    sentences: tuple[Sentence, ...]
    signals: tuple[ContentSignal, ...]
    loudness: LoudnessEnvelope | None
    scene_cuts: tuple[SceneCut, ...] | None
    """None when scene detection did not run (no proxy, disabled or failed)."""
    language: str | None
    lexicon_supported: bool
    """True when wording signals (stories, humor, advice...) were looked for."""


@dataclass(frozen=True, slots=True)
class ClipSpan:
    """A labelled part of a candidate (hook, context or payoff)."""

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    """A proposed clip before scoring."""

    start_s: float
    end_s: float
    first_sentence: int
    last_sentence: int
    methods: tuple[str, ...]
    anchors: tuple[int, ...]
    """Indices into ``Evidence.signals`` that caused this proposal."""

    @property
    def duration_s(self) -> float:
        """Length in seconds."""
        return self.end_s - self.start_s


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One factor of a candidate's score."""

    name: str
    label: str
    value: float
    """0 (poor) to 1 (strong)."""
    weight: float
    detail: str


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A scored, explainable clip suggestion (not yet stored)."""

    start_s: float
    end_s: float
    category: str
    methods: tuple[str, ...]
    title: str
    excerpt: str
    hook: ClipSpan
    context: ClipSpan | None
    payoff: ClipSpan
    score: float
    """0 to 100."""
    confidence: float
    """Evidence quality, 0 to 1."""
    components: tuple[ScoreComponent, ...]
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    explanation: str
    signal_indices: tuple[int, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        """Length in seconds."""
        return self.end_s - self.start_s


class ClipDecision(StrEnum):
    """The user's verdict on a suggestion."""

    FAVORITE = "favorite"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ClipAnalysis:
    """A stored clip-intelligence run for one video."""

    id: int
    video_id: int
    transcript_id: int
    job_id: int | None
    analysis_version: int
    scoring_version: int
    params_hash: str
    params: dict[str, Any]
    duration_s: float
    sources: dict[str, Any]
    """What was analysed per evidence source, with notes on anything skipped."""
    stats: dict[str, Any]
    signal_count: int
    candidate_count: int
    processing_s: float | None
    is_current: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredSignal:
    """A signal as stored for an analysis."""

    id: int
    analysis_id: int
    position: int
    kind: SignalKind
    source: SignalSource
    start_s: float
    end_s: float
    strength: float
    confidence: float
    label: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClipCandidate:
    """A stored, ranked clip suggestion with the user's decision (if any)."""

    id: int
    analysis_id: int
    video_id: int
    key: str
    """Stable id derived from the video's SHA-256 and the boundaries."""
    rank: int
    start_s: float
    end_s: float
    category: str
    methods: tuple[str, ...]
    title: str
    excerpt: str
    hook: ClipSpan
    context: ClipSpan | None
    payoff: ClipSpan
    score: float
    confidence: float
    components: tuple[ScoreComponent, ...]
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    explanation: str
    signal_positions: tuple[int, ...]
    evidence: dict[str, Any]
    decision: ClipDecision | None = None
    adjusted_start_s: float | None = None
    adjusted_end_s: float | None = None
    decided_at: str | None = None

    @property
    def duration_s(self) -> float:
        """Suggested length in seconds."""
        return self.end_s - self.start_s

    @property
    def effective_start_s(self) -> float:
        """Start the next phases should use (the user's adjustment, if any)."""
        return self.adjusted_start_s if self.adjusted_start_s is not None else self.start_s

    @property
    def effective_end_s(self) -> float:
        """End the next phases should use (the user's adjustment, if any)."""
        return self.adjusted_end_s if self.adjusted_end_s is not None else self.end_s
