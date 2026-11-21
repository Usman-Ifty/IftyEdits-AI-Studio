"""Validation and normalisation of speech-engine output. Pure functions, no I/O.

Engines occasionally return timings slightly outside the audio, words that spill past
their segment, empty segments or (rarely) non-finite numbers. Everything stored passes
through :func:`normalize_segments`, which guarantees:

* every time is finite, non-negative and rounded to the millisecond;
* ``end_s >= start_s`` for segments and words;
* nothing ends after the audio duration (when it is known);
* segments are ordered by start time and words by start time within their segment;
* every word lies inside its segment;
* text is stripped and never empty.

Segments that cannot be repaired (non-finite times, end before start, starting after
the audio ends) are dropped and counted. Overlapping segments are kept: overlapping
speech and overlapping subtitle cues are both legitimate.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from iftyedits.core.languages import WHISPER_LANGUAGES
from iftyedits.core.models import TranscriptSegment, TranscriptWord

DURATION_TOLERANCE_S = 0.5
"""Timings up to this far past the audio end are clamped instead of dropped."""

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class NormalizedTranscript:
    """Validated segments plus how many engine segments were unusable."""

    segments: tuple[TranscriptSegment, ...]
    dropped: int

    @property
    def word_count(self) -> int:
        """Number of timed words across all segments."""
        return sum(len(s.words) for s in self.segments)


def clean_text(text: str) -> str:
    """Collapse runs of whitespace (including newlines) and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _ms(value: float) -> float:
    return round(value, 3)


def _usable(start: float, end: float, duration: float | None) -> bool:
    if not (math.isfinite(start) and math.isfinite(end)):
        return False
    if end < start:
        return False
    return not (duration is not None and start > duration + DURATION_TOLERANCE_S)


def _limit(duration: float | None) -> float:
    return duration if duration is not None else math.inf


def _normalize_words(
    words: Iterable[TranscriptWord], seg_start: float, seg_end: float
) -> tuple[TranscriptWord, ...]:
    cleaned: list[TranscriptWord] = []
    for word in words:
        text = clean_text(word.text)
        if not text or not _usable(word.start_s, word.end_s, None):
            continue
        start = _ms(min(max(word.start_s, seg_start), seg_end))
        end = _ms(min(max(word.end_s, start), seg_end))
        probability = word.probability
        if probability is not None and not math.isfinite(probability):
            probability = None
        cleaned.append(TranscriptWord(start, end, text, probability))
    cleaned.sort(key=lambda w: (w.start_s, w.end_s))
    return tuple(cleaned)


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def normalize_segments(
    segments: Iterable[TranscriptSegment], duration_s: float | None
) -> NormalizedTranscript:
    """Validate, repair and order engine segments. See the module docstring for rules."""
    duration = duration_s if duration_s is not None and duration_s > 0 else None
    limit = _limit(duration)
    kept: list[TranscriptSegment] = []
    dropped = 0
    for segment in segments:
        text = clean_text(segment.text)
        if not text:
            continue
        if not _usable(segment.start_s, segment.end_s, duration):
            dropped += 1
            continue
        start = _ms(min(max(segment.start_s, 0.0), limit))
        end = _ms(min(max(segment.end_s, start), limit))
        kept.append(
            TranscriptSegment(
                start_s=start,
                end_s=end,
                text=text,
                words=_normalize_words(segment.words, start, end),
                avg_logprob=_finite_or_none(segment.avg_logprob),
                no_speech_prob=_finite_or_none(segment.no_speech_prob),
            )
        )
    kept.sort(key=lambda s: (s.start_s, s.end_s))
    return NormalizedTranscript(tuple(kept), dropped)


def validate_language(code: str | None) -> str | None:
    """Normalise a language request: ``None``/``""``/``"auto"`` mean detect automatically.

    Returns a lower-case code (``"en"``, ``"yue"``) or raises ``ValueError``.
    """
    if code is None:
        return None
    value = code.strip().lower()
    if value in ("", "auto"):
        return None
    if value not in WHISPER_LANGUAGES:
        raise ValueError(
            f"Unsupported language code {code!r}; use 'auto' or a code such as 'en', 'ur', 'es'"
        )
    return value
