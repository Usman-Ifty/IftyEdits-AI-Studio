"""Candidate clip generation (Phase 2B): meaningful, sentence-aligned clip proposals.

Candidates are grown around evidence, never cut at fixed intervals:

- **Anchors**: each interesting signal (question and answer, story, hook, humor,
  surprise, emotional wording, advice, motivation, rapid exchange, volume rise) proposes
  clips around it. Reactions and punchlines get setup before them; questions, stories
  and hooks start the clip.
- **Topic blocks**: a stretch between two topic changes that already has Shorts length.
- **Thought starts**: a sentence after a pause that does not open with a continuation
  word ("and", "but", "so"...) starts a self-contained thought.

Intros, calls to action and sign-offs ("welcome back", "like and subscribe", "thanks for
watching") are poor starts and ends, so boundaries avoid them when the clip can end earlier.

Boundaries always fall on sentence edges (word timings when available), a little
before the first word and after the last, inside the silence between sentences (never
inside a word), snapped to a nearby scene change when one falls in that silence. Ends
prefer full stops followed by a pause and avoid crossing a topic change. Duplicates and
near-duplicates are merged. The output is sorted and fully deterministic.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from iftyedits.core.clip_models import CandidateDraft, ContentSignal, Evidence, Sentence, SignalKind
from iftyedits.core.signals import (
    boilerplate_match,
    boilerplate_share,
    is_question,
    starts_with_connector,
)

CANDIDATES_VERSION = 2
"""Bump when generation rules change, so stored analyses are not reused."""

ANCHOR_KINDS = frozenset(
    {
        SignalKind.QUESTION_ANSWER,
        SignalKind.STORY,
        SignalKind.HOOK,
        SignalKind.INSIGHT,
        SignalKind.MOTIVATION,
        SignalKind.EMOTIONAL_WORDING,
        SignalKind.HUMOR_CUE,
        SignalKind.SURPRISE,
        SignalKind.RAPID_EXCHANGE,
        SignalKind.LOUDNESS_PEAK,
    }
)
LEADING_KINDS = frozenset(
    {SignalKind.QUESTION_ANSWER, SignalKind.STORY, SignalKind.HOOK, SignalKind.INSIGHT,
     SignalKind.MOTIVATION}
)  # fmt: skip
"""Anchors that open a clip; the others (reactions, punchlines) need setup before them."""
LEAD_IN_S = 0.2
TAIL_S = 0.35
SNAP_S = 0.6
NEAR_DUPLICATE_S = 1.0
MIN_SPEECH_S = 3.0
PURE_BOILERPLATE_SHARE = 0.6
"""A whole topic block this promotional is an intro or outro, not a clip."""


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """Clip length preferences (seconds) and how strictly they apply."""

    min_s: float = 15.0
    target_s: float = 40.0
    max_s: float = 60.0

    def __post_init__(self) -> None:
        if not 0 < self.min_s <= self.target_s <= self.max_s:
            raise ValueError(
                "Clip durations must satisfy 0 < minimum <= target <= maximum "
                f"(got {self.min_s}, {self.target_s}, {self.max_s})"
            )

    @property
    def floor_s(self) -> float:
        """Shortest proposal kept (a little under the minimum, penalised in scoring)."""
        return self.min_s * 0.75

    @property
    def ceiling_s(self) -> float:
        """Longest proposal kept (a little over the maximum, penalised in scoring)."""
        return self.max_s * 1.15


class _Timeline:
    """Sentence lookups, boundary rules and topic edges for one video."""

    def __init__(self, evidence: Evidence) -> None:
        self.evidence = evidence
        self.sentences = evidence.sentences
        self.starts = [s.start_s for s in self.sentences]
        self.hooks = {
            int(s.evidence["sentence"])
            for s in evidence.signals
            if s.kind is SignalKind.HOOK and "sentence" in s.evidence
        }
        self.topic_after = {
            int(s.evidence["after_sentence"])
            for s in evidence.signals
            if s.kind is SignalKind.TOPIC_SHIFT and "after_sentence" in s.evidence
        }
        self.boilerplate = [boilerplate_match(s) is not None for s in self.sentences]
        self.cuts = sorted(c.time_s for c in evidence.scene_cuts or ())

    def index_at(self, t: float) -> int:
        """Sentence containing ``t``, else the next one (clamped to the last)."""
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and self.sentences[i].end_s > t:
            return i
        return min(len(self.sentences) - 1, max(0, i + 1))

    def last_index_before(self, t: float) -> int:
        """Last sentence that starts before ``t``."""
        return max(0, bisect.bisect_left(self.starts, t) - 1)

    def start_quality(self, i: int) -> float:
        s = self.sentences[i]
        quality = 0.0
        if i == 0 or s.gap_before_s >= 0.5 or (i - 1) in self.topic_after:
            quality += 0.4
        if not starts_with_connector(s.text):
            quality += 0.3
        if i in self.hooks or is_question(s):
            quality += 0.2
        if i > 0 and not self.sentences[i - 1].terminal:
            quality -= 0.3  # the previous "sentence" was cut by a pause, not finished
        if self.boilerplate[i]:
            quality -= 0.5  # never open a clip on an intro or a call to action
        return quality

    def end_quality(self, j: int) -> float:
        s = self.sentences[j]
        quality = 0.5 if s.terminal else 0.0
        if s.gap_after_s >= 0.5:
            quality += 0.3
        if not is_question(s):
            quality += 0.2
        if self.boilerplate[j]:
            quality -= 0.5  # end before the sign-off or call to action when possible
        return quality

    def crosses_topic(self, i: int, j: int) -> bool:
        return any(i <= k < j for k in self.topic_after)

    def start_time(self, i: int) -> float:
        s = self.sentences[i]
        floor = self.sentences[i - 1].end_s + 0.05 if i > 0 else 0.0
        start = max(floor, s.start_s - LEAD_IN_S, 0.0)
        snapped = [c for c in self.cuts if floor <= c <= s.start_s and s.start_s - c <= SNAP_S]
        return round(snapped[-1] if snapped else start, 3)

    def end_time(self, j: int) -> float:
        s = self.sentences[j]
        last = j + 1 >= len(self.sentences)
        ceiling = self.evidence.duration_s if last else self.sentences[j + 1].start_s - 0.05
        end = min(ceiling, s.end_s + TAIL_S) if ceiling > s.end_s else s.end_s
        snapped = [c for c in self.cuts if s.end_s <= c <= ceiling and c - s.end_s <= SNAP_S]
        return round(snapped[0] if snapped else end, 3)


def _end_choices(
    timeline: _Timeline, first: int, must_reach: int, config: CandidateConfig
) -> list[int]:
    """Best end sentence, plus a shorter alternative when one is clearly different."""
    sentences = timeline.sentences
    start = timeline.start_time(first)
    scored: list[tuple[float, int]] = []
    for j in range(max(first, must_reach), len(sentences)):
        length = timeline.end_time(j) - start
        if length > config.ceiling_s:
            break
        if length < config.floor_s:
            continue
        fit = 1.0 - min(1.0, abs(length - config.target_s) / config.target_s)
        value = timeline.end_quality(j) + 0.6 * fit
        if timeline.crosses_topic(first, j):
            value -= 0.5
        scored.append((value, j))
    if not scored:
        return []
    best = max(scored, key=lambda item: (item[0], -item[1]))[1]
    choices = [best]
    best_length = timeline.end_time(best) - start
    shorter = [
        (v, j)
        for v, j in scored
        if timeline.end_time(j) - start <= best_length - max(8.0, config.min_s * 0.5)
    ]
    if shorter:
        choices.append(max(shorter, key=lambda item: (item[0], -item[1]))[1])
    return choices


def _start_choice(
    timeline: _Timeline, signal: ContentSignal, first: int, config: CandidateConfig
) -> int:
    """Where to start for an anchor: at it, or a little earlier for setup or context."""
    sentences = timeline.sentences
    leading = signal.kind in LEADING_KINDS
    lookback = 6.0 if leading else config.target_s * 0.6
    options = [
        i
        for i in range(first, -1, -1)
        if signal.start_s - sentences[i].start_s <= lookback
        and not timeline.crosses_topic(i, first)
    ] or [first]

    def value(i: int) -> tuple[float, int]:
        context = 0.0 if leading else 0.15 * min(1.0, (signal.start_s - sentences[i].start_s) / 8.0)
        return timeline.start_quality(i) + context - (0.05 * (first - i) if leading else 0.0), i

    if signal.kind is SignalKind.HOOK or (leading and timeline.start_quality(first) >= 0.6):
        return first  # a hook is the opening line by definition
    return max(options, key=value)


def _draft(
    timeline: _Timeline, first: int, last: int, method: str, anchors: tuple[int, ...]
) -> CandidateDraft:
    return CandidateDraft(
        start_s=timeline.start_time(first),
        end_s=timeline.end_time(last),
        first_sentence=first,
        last_sentence=last,
        methods=(method,),
        anchors=anchors,
    )


def generate_candidates(
    evidence: Evidence, config: CandidateConfig | None = None
) -> list[CandidateDraft]:
    """Propose clips for one video. Empty when the transcript has no usable speech."""
    config = config or CandidateConfig()
    timeline = _Timeline(evidence)
    sentences = timeline.sentences
    if not sentences:
        return []
    speech_span = sentences[-1].end_s - sentences[0].start_s
    if speech_span < MIN_SPEECH_S:
        return []
    drafts: list[CandidateDraft] = []
    if speech_span < config.floor_s:
        return [_draft(timeline, 0, len(sentences) - 1, "whole_video", ())]

    for position, signal in enumerate(evidence.signals):
        if signal.kind not in ANCHOR_KINDS:
            continue
        first = timeline.index_at(signal.start_s)
        reach = max(first, timeline.last_index_before(signal.end_s))
        start = _start_choice(timeline, signal, first, config)
        for end in _end_choices(timeline, start, reach, config):
            drafts.append(_draft(timeline, start, end, f"anchor:{signal.kind.value}", (position,)))

    edges = sorted({-1, *timeline.topic_after, len(sentences) - 1})
    for a, b in itertools.pairwise(edges):
        first, last = a + 1, b
        if first > last:
            continue
        length = timeline.end_time(last) - timeline.start_time(first)
        if config.floor_s <= length <= config.ceiling_s and (
            boilerplate_share(sentences[first : last + 1]) < PURE_BOILERPLATE_SHARE
        ):
            drafts.append(_draft(timeline, first, last, "topic_block", ()))

    for i in range(len(sentences)):
        if timeline.start_quality(i) >= 0.7:
            for end in _end_choices(timeline, i, i, config)[:1]:
                drafts.append(_draft(timeline, i, end, "thought_start", ()))

    return merge_duplicates(drafts)


def merge_duplicates(drafts: Sequence[CandidateDraft]) -> list[CandidateDraft]:
    """Merge proposals with (nearly) the same boundaries, combining their methods."""
    merged: list[CandidateDraft] = []
    for draft in sorted(drafts, key=lambda d: (d.start_s, d.end_s, d.methods)):
        if draft.end_s <= draft.start_s:
            continue
        twin_at: int | None = None
        for k in range(len(merged) - 1, -1, -1):  # merged is ordered by start
            if draft.start_s - merged[k].start_s >= NEAR_DUPLICATE_S:
                break
            if abs(merged[k].end_s - draft.end_s) < NEAR_DUPLICATE_S:
                twin_at = k
                break
        if twin_at is None:
            merged.append(draft)
            continue
        twin = merged[twin_at]
        merged[twin_at] = CandidateDraft(
            start_s=twin.start_s,
            end_s=twin.end_s,
            first_sentence=twin.first_sentence,
            last_sentence=twin.last_sentence,
            methods=tuple(sorted(set(twin.methods) | set(draft.methods))),
            anchors=tuple(sorted(set(twin.anchors) | set(draft.anchors))),
        )
    return merged


def sentences_in(evidence: Evidence, draft: CandidateDraft) -> Sequence[Sentence]:
    """The sentences a draft covers."""
    return evidence.sentences[draft.first_sentence : draft.last_sentence + 1]
