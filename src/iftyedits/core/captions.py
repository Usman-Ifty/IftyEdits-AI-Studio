"""Turn transcript segments into clip-relative caption cues (Phase 3).

Source-video time becomes clip time by subtracting the clip start; nothing is shown
before 0 or after the clip's end. Timing is honest about where it came from:

- ``word``: the transcript has word timings and the cue uses them;
- ``segment``: the transcript only has a phrase timing, so a short phrase is shown as is;
- ``estimated``: a long phrase had to be split, or text was corrected by the user, so the
  split points are spread over the phrase's real duration by letter count.

Active-word highlighting needs ``word`` timing and is switched off for other cues.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from iftyedits.core.models import StoredSegment, TranscriptSegment, TranscriptWord
from iftyedits.core.shorts import CaptionStyle

Timing = Literal["word", "segment", "estimated"]
MIN_CUE_S = 0.2
MAX_CUE_S = 6.0
PAUSE_SPLIT_S = 0.7
WORD_TOLERANCE_S = 0.35
_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")
_CLAUSE_END = re.compile(r"[,;:\u2013\u2014-]$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class CueWord:
    """One word of a cue with clip-relative timing."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """One caption shown from ``start_s`` to ``end_s`` (seconds from the clip start)."""

    start_s: float
    end_s: float
    text: str
    words: tuple[CueWord, ...]
    timing: Timing
    segment: int
    """Transcript segment position the text came from (for corrections)."""

    @property
    def can_highlight(self) -> bool:
        """True when each word has its own real timing."""
        return self.timing == "word" and len(self.words) > 0


def clean_text(text: str) -> str:
    """Collapse whitespace and drop control characters; keeps punctuation and Unicode."""
    return " ".join(_CONTROL.sub("", text).split())


def _reliable_words(segment: StoredSegment | TranscriptSegment) -> tuple[TranscriptWord, ...]:
    """The segment's words if their timings are ordered and fit the segment, else ()."""
    words = tuple(w for w in segment.words if w.text.strip())
    if not words:
        return ()
    previous = segment.start_s - WORD_TOLERANCE_S
    for word in words:
        if word.end_s < word.start_s or word.start_s < previous - 1e-6:
            return ()
        if word.start_s < segment.start_s - WORD_TOLERANCE_S:
            return ()
        if word.end_s > segment.end_s + WORD_TOLERANCE_S + 2.0:
            return ()
        previous = word.start_s
    return words


def _estimated_words(text: str, start_s: float, end_s: float) -> list[tuple[str, float, float]]:
    """Spread the words of ``text`` over a span by letter count (a stated estimate)."""
    parts = text.split()
    if not parts:
        return []
    weights = [len(p) + 1 for p in parts]
    total = float(sum(weights))
    out: list[tuple[str, float, float]] = []
    cursor = start_s
    for part, weight in zip(parts, weights, strict=True):
        step = (end_s - start_s) * weight / total
        out.append((part, cursor, cursor + step))
        cursor += step
    return out


def _chunks(
    words: Sequence[tuple[str, float, float]], max_chars: int
) -> list[list[tuple[str, float, float]]]:
    """Split words into display chunks: sentence ends, pauses, length and cue duration."""
    chunks: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for index, word in enumerate(words):
        text, start, _ = word
        if current:
            length = sum(len(w[0]) + 1 for w in current) + len(text)
            pause = start - current[-1][2]
            too_long = length > max_chars
            too_slow = start - current[0][1] > MAX_CUE_S
            if too_long or too_slow or pause > PAUSE_SPLIT_S:
                chunks.append(current)
                current = []
        current.append(word)
        last = index == len(words) - 1
        ends_sentence = bool(_SENTENCE_END.search(text)) and len(current) >= 3
        ends_clause = bool(_CLAUSE_END.search(text)) and sum(len(w[0]) + 1 for w in current) > (
            max_chars * 0.6
        )
        if last or ends_sentence or ends_clause:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build_cues(
    segments: Sequence[StoredSegment],
    start_s: float,
    end_s: float,
    style: CaptionStyle,
    text_overrides: dict[int, str] | None = None,
) -> list[CaptionCue]:
    """Captions for the transcript span ``[start_s, end_s]``, in clip time.

    ``text_overrides`` maps a segment's position to corrected text. Corrected segments
    lose word timing (their words are re-spread over the phrase) and are marked as such.
    """
    overrides = text_overrides or {}
    duration = end_s - start_s
    max_chars = max(
        12, int(22 * style.max_lines * style.width_fraction / 0.86 * 64 / style.font_size)
    )
    cues: list[CaptionCue] = []
    for seg in segments:
        if seg.end_s <= start_s or seg.start_s >= end_s:
            continue
        override = overrides.get(seg.position)
        timing: Timing
        words: list[tuple[str, float, float]]
        if override is not None:
            text = clean_text(override)
            words = _estimated_words(text, seg.start_s, seg.end_s)
            timing = "estimated"
        else:
            real = _reliable_words(seg)
            if real:
                words = [
                    (clean_text(w.text), w.start_s, max(w.end_s, w.start_s + 0.05)) for w in real
                ]
                words = [w for w in words if w[0]]
                timing = "word"
            else:
                text = clean_text(seg.text)
                words = _estimated_words(text, seg.start_s, seg.end_s)
                timing = "segment"
        # keep only words that are mostly inside the clip
        kept = [w for w in words if start_s <= (w[1] + w[2]) / 2 <= end_s]
        if not kept:
            continue
        pieces = _chunks(kept, max_chars)
        if timing == "segment" and len(pieces) > 1:
            timing = "estimated"
        for piece in pieces:
            cue_start = max(0.0, piece[0][1] - start_s)
            cue_end = min(duration, piece[-1][2] - start_s)
            if cue_end - cue_start < MIN_CUE_S:
                cue_end = min(duration, cue_start + MIN_CUE_S)
            if cue_end <= cue_start:
                continue
            cue_words = tuple(
                CueWord(
                    w[0],
                    min(max(0.0, w[1] - start_s), duration),
                    min(max(0.0, w[2] - start_s), duration),
                )
                for w in piece
            )
            cues.append(
                CaptionCue(
                    start_s=round(cue_start, 3),
                    end_s=round(cue_end, 3),
                    text=" ".join(w[0] for w in piece),
                    words=cue_words,
                    timing=timing,
                    segment=seg.position,
                )
            )
    return _no_overlaps(cues)


def _no_overlaps(cues: list[CaptionCue]) -> list[CaptionCue]:
    """Sort cues and trim each so it ends where the next begins."""
    ordered = sorted(cues, key=lambda c: (c.start_s, c.end_s))
    fixed: list[CaptionCue] = []
    for index, cue in enumerate(ordered):
        end = cue.end_s
        if index + 1 < len(ordered) and ordered[index + 1].start_s < end:
            end = ordered[index + 1].start_s
        if end - cue.start_s < 0.05:
            continue
        fixed.append(
            CaptionCue(cue.start_s, round(end, 3), cue.text, cue.words, cue.timing, cue.segment)
        )
    return fixed


def timing_summary(cues: Sequence[CaptionCue]) -> str:
    """One line saying how reliable the caption timing is."""
    if not cues:
        return "No speech in this clip, so there are no captions."
    counts = {t: sum(1 for c in cues if c.timing == t) for t in ("word", "segment", "estimated")}
    parts = []
    if counts["word"]:
        parts.append(f"{counts['word']} with word timing")
    if counts["segment"]:
        parts.append(f"{counts['segment']} with phrase timing")
    if counts["estimated"]:
        parts.append(f"{counts['estimated']} with estimated split points")
    return f"{len(cues)} captions: " + ", ".join(parts) + "."
