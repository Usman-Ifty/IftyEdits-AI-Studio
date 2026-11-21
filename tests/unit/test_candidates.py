"""Phase 2B candidate generation: boundaries, anchors, duplicates and edge cases."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from iftyedits.core.candidates import (
    CandidateConfig,
    generate_candidates,
    merge_duplicates,
    sentences_in,
)
from iftyedits.core.clip_models import CandidateDraft, Evidence, SceneCut, SignalKind
from iftyedits.core.models import TranscriptSegment
from iftyedits.core.signals import build_evidence
from tests.clip_fakes import podcast, script


def evidence_for(
    segments: Sequence[TranscriptSegment],
    *,
    language: str | None = "en",
    cuts: Sequence[SceneCut] | None = None,
) -> Evidence:
    duration = (segments[-1].end_s + 2.0) if segments else 30.0
    return build_evidence(
        segments, duration_s=duration, language=language, loudness=None, scene_cuts=cuts
    )


@pytest.fixture(scope="module")
def evidence() -> Evidence:
    return evidence_for(podcast())


@pytest.fixture(scope="module")
def drafts(evidence: Evidence) -> list[CandidateDraft]:
    return generate_candidates(evidence)


def test_candidates_respect_duration_bounds(drafts: list[CandidateDraft]) -> None:
    config = CandidateConfig()
    assert len(drafts) >= 5
    for d in drafts:
        assert config.floor_s <= d.duration_s <= config.ceiling_s + 0.5
        assert d.first_sentence <= d.last_sentence


def test_boundaries_never_cut_words_or_sentences(
    evidence: Evidence, drafts: list[CandidateDraft]
) -> None:
    words = [w for seg in podcast() for w in seg.words]
    for d in drafts:
        assert not any(w.start_s < d.start_s < w.end_s for w in words)
        assert not any(w.start_s < d.end_s < w.end_s for w in words)
        first = evidence.sentences[d.first_sentence]
        last = evidence.sentences[d.last_sentence]
        assert d.start_s <= first.start_s and d.end_s >= last.end_s
        if d.first_sentence > 0:
            assert d.start_s > evidence.sentences[d.first_sentence - 1].end_s
        if d.last_sentence + 1 < len(evidence.sentences):
            assert d.end_s < evidence.sentences[d.last_sentence + 1].start_s


def test_question_and_answer_is_kept_whole(
    evidence: Evidence, drafts: list[CandidateDraft]
) -> None:
    qa = next(s for s in evidence.signals if s.kind is SignalKind.QUESTION_ANSWER)
    covering = [
        d
        for d in drafts
        if "anchor:question_answer" in d.methods and d.start_s <= qa.start_s and d.end_s >= qa.end_s
    ]
    assert covering


def test_reactions_get_setup_before_them(evidence: Evidence, drafts: list[CandidateDraft]) -> None:
    reaction = next(
        s
        for s in evidence.signals
        if s.kind is SignalKind.SURPRISE and "no way" in s.evidence["matched"]
    )
    position = evidence.signals.index(reaction)
    anchored = [d for d in drafts if position in d.anchors]
    assert anchored
    assert all(d.start_s < reaction.start_s - 5.0 for d in anchored)


def test_duplicates_are_merged_and_output_is_deterministic(
    evidence: Evidence, drafts: list[CandidateDraft]
) -> None:
    for i, a in enumerate(drafts):
        for b in drafts[i + 1 :]:
            assert abs(a.start_s - b.start_s) >= 1.0 or abs(a.end_s - b.end_s) >= 1.0
    assert generate_candidates(evidence) == drafts
    assert drafts == sorted(drafts, key=lambda d: (d.start_s, d.end_s, d.methods))


def test_merge_combines_methods_and_drops_invalid() -> None:
    a = CandidateDraft(10.0, 40.0, 0, 3, ("topic_block",), ())
    b = CandidateDraft(10.4, 40.6, 0, 3, ("anchor:story",), (4,))
    broken = CandidateDraft(50.0, 50.0, 5, 5, ("thought_start",), ())
    merged = merge_duplicates([b, a, broken])
    assert merged == [CandidateDraft(10.0, 40.0, 0, 3, ("anchor:story", "topic_block"), (4,))]


def test_topic_blocks_become_candidates() -> None:
    cooking = [
        f"Add the {w} to the pan with garlic butter and onions, stir the sauce gently."
        for w in ("tomatoes", "pepper", "basil", "pasta", "cheese", "spinach", "mushrooms")
    ]
    rockets = [
        f"The {w} engine burns liquid oxygen while the booster rocket climbs into orbit."
        for w in ("first", "second", "third", "upper", "main", "backup", "final")
    ]
    evidence = evidence_for(script(cooking + rockets, gap=0.3))
    blocks = [d for d in generate_candidates(evidence) if "topic_block" in d.methods]
    assert [(d.first_sentence, d.last_sentence) for d in blocks] == [(0, 6), (7, 13)]


def test_empty_short_and_tiny_transcripts() -> None:
    assert generate_candidates(evidence_for(())) == []
    assert generate_candidates(evidence_for(script(["Hi there."]))) == []  # under 3 s
    short = generate_candidates(
        evidence_for(script(["One line here now.", "And one more line."] * 2))
    )
    assert [d.methods for d in short] == [("whole_video",)]


def test_missing_word_timings_still_produce_aligned_candidates() -> None:
    evidence = evidence_for(podcast(with_words=False))
    drafts = generate_candidates(evidence)
    assert drafts
    for d in drafts:
        assert d.start_s <= evidence.sentences[d.first_sentence].start_s
        assert d.end_s >= evidence.sentences[d.last_sentence].end_s


def test_boundaries_snap_to_scene_cuts_inside_the_silence() -> None:
    segments = podcast()
    plain = evidence_for(segments)
    target = plain.sentences[16]  # "Here's the craziest thing..." (a hook)
    cut = round(target.start_s - 0.3, 3)
    snapped = evidence_for(segments, cuts=[SceneCut(cut, 0.9)])
    starts = {d.start_s for d in generate_candidates(snapped) if d.first_sentence == 16}
    assert cut in starts


def test_custom_durations_and_validation() -> None:
    evidence = evidence_for(podcast())
    short = generate_candidates(evidence, CandidateConfig(min_s=8, target_s=12, max_s=20))
    assert short and all(d.duration_s <= 20 * 1.15 + 0.5 for d in short)
    assert all(len(sentences_in(evidence, d)) >= 1 for d in short)
    with pytest.raises(ValueError, match="minimum <= target <= maximum"):
        CandidateConfig(min_s=30, target_s=20, max_s=60)
