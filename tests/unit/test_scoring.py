"""Phase 2C scoring and ranking: components, penalties, determinism, feedback, selection."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from iftyedits.core.candidates import CandidateConfig, generate_candidates
from iftyedits.core.clip_models import CandidateDraft, Evidence, ScoredCandidate
from iftyedits.core.models import TranscriptSegment
from iftyedits.core.scoring import (
    CATEGORY_LABELS,
    DEFAULT_WEIGHTS,
    SCORING_VERSION,
    Feedback,
    RankingConfig,
    ScoringWeights,
    candidate_key,
    category_bias,
    overlap_fraction,
    rank_candidates,
    score_candidates,
    score_draft,
)
from iftyedits.core.signals import build_evidence
from tests.clip_fakes import podcast, script


def evidence_for(segments: Sequence[TranscriptSegment], *, language: str | None = "en") -> Evidence:
    duration = (segments[-1].end_s + 2.0) if segments else 30.0
    return build_evidence(
        segments, duration_s=duration, language=language, loudness=None, scene_cuts=None
    )


CONFIG = CandidateConfig()


@pytest.fixture(scope="module")
def evidence() -> Evidence:
    return evidence_for(podcast())


@pytest.fixture(scope="module")
def scored(evidence: Evidence) -> list[ScoredCandidate]:
    return score_candidates(evidence, generate_candidates(evidence, CONFIG), CONFIG)


def test_every_candidate_is_scored_with_a_full_breakdown(scored: list[ScoredCandidate]) -> None:
    assert len(scored) >= 5
    for c in scored:
        assert 0.0 <= c.score <= 100.0
        assert 0.0 <= c.confidence <= 1.0
        assert [x.name for x in c.components] == list(DEFAULT_WEIGHTS)
        assert all(0.0 <= x.value <= 1.0 for x in c.components)
        assert c.category in CATEGORY_LABELS
        assert c.title and c.excerpt and c.explanation
        assert "algorithmic" in c.explanation
        assert c.hook.start_s >= c.start_s and c.payoff.end_s <= c.end_s
        assert c.evidence["scoring_version"] == SCORING_VERSION


def test_scores_are_reproducible(evidence: Evidence, scored: list[ScoredCandidate]) -> None:
    again = score_candidates(evidence, generate_candidates(evidence, CONFIG), CONFIG)
    assert again == scored
    assert rank_candidates(again) == rank_candidates(scored)


def test_score_matches_weighted_components_minus_penalties(scored: list[ScoredCandidate]) -> None:
    for c in scored:
        total = sum(x.weight for x in c.components)
        base = 100.0 * sum(x.value * x.weight for x in c.components) / total
        expected = max(0.0, min(100.0, base - c.evidence["penalty_points"]))
        assert c.score == pytest.approx(expected, abs=0.01)


def test_changing_weights_changes_scores_but_not_validity(evidence: Evidence) -> None:
    draft = generate_candidates(evidence, CONFIG)[0]
    default = score_draft(evidence, draft, CONFIG)
    only_duration = ScoringWeights({**{k: 0.0 for k in DEFAULT_WEIGHTS}, "duration_fit": 1.0})
    shifted = score_draft(evidence, draft, CONFIG, only_duration)
    duration_value = next(c.value for c in shifted.components if c.name == "duration_fit")
    assert shifted.evidence["base_score"] == pytest.approx(100 * duration_value, abs=0.01)
    assert shifted.score != default.score


def test_weight_validation() -> None:
    with pytest.raises(ValueError, match="exactly"):
        ScoringWeights({"hook": 1.0})
    with pytest.raises(ValueError, match="non-negative"):
        ScoringWeights({**DEFAULT_WEIGHTS, "hook": -0.1})
    with pytest.raises(ValueError, match="positive total"):
        ScoringWeights({k: 0.0 for k in DEFAULT_WEIGHTS})
    with pytest.raises(ValueError, match="at least 1"):
        RankingConfig(max_count=0)
    with pytest.raises(ValueError, match="between 0 and 1"):
        RankingConfig(max_overlap=1.5)


def test_a_clean_hook_story_beats_a_mid_thought_fragment(evidence: Evidence) -> None:
    drafts = generate_candidates(evidence, CONFIG)
    story = next(d for d in drafts if d.first_sentence == 16 and d.end_s - d.start_s > 20)
    good = score_draft(evidence, story, CONFIG)
    sentences = evidence.sentences
    # Same time range but starting on a continuation sentence and ending mid-story.
    bad_first = next(
        s.index for s in sentences if s.text.strip().lower().startswith("and the second")
    )
    broken = CandidateDraft(
        sentences[bad_first].start_s - 0.2,
        sentences[bad_first + 2].end_s + 0.1,
        bad_first,
        bad_first + 2,
        ("test",),
        (),
    )
    weak = score_draft(evidence, broken, CONFIG)
    assert good.score > weak.score
    assert weak.evidence["penalties"]
    assert any("Weak opening" in w or "Weak start" in w for w in weak.weaknesses)


def test_hook_component_sees_hook_wording(evidence: Evidence) -> None:
    drafts = generate_candidates(evidence, CONFIG)
    hooked = next(d for d in drafts if d.first_sentence == 16)
    comp = {c.name: c for c in score_draft(evidence, hooked, CONFIG).components}
    assert comp["hook"].value >= 0.7
    assert "opens with" in comp["hook"].detail


def test_context_is_reported_when_the_hook_comes_after_setup(evidence: Evidence) -> None:
    # A clip that starts a few sentences before the hook line has setup before the hook.
    start = evidence.sentences[14]
    draft = CandidateDraft(
        start.start_s - 0.2, evidence.sentences[24].end_s + 0.3, 14, 24, ("t",), ()
    )
    result = score_draft(evidence, draft, CONFIG)
    assert result.hook.start_s >= result.start_s
    if result.context is not None:
        assert result.context.end_s <= result.hook.start_s


def test_non_english_skips_wording_components() -> None:
    evidence = evidence_for(podcast(), language="fr")
    draft = generate_candidates(evidence, CONFIG)[0]
    result = score_draft(evidence, draft, CONFIG)
    by_name = {c.name: c for c in result.components}
    for name in ("emotion", "surprise", "usefulness"):
        assert by_name[name].weight == 0.0
        assert "Not assessed" in by_name[name].detail
    assert result.confidence < score_draft(evidence_for(podcast()), draft, CONFIG).confidence


def test_unreliable_transcript_is_penalised_and_less_confident() -> None:
    clear = evidence_for(podcast())
    murky = evidence_for(podcast(probability=0.3))
    draft = generate_candidates(clear, CONFIG)[0]
    a = score_draft(clear, draft, CONFIG)
    b = score_draft(murky, draft, CONFIG)
    assert b.score < a.score
    assert b.confidence < a.confidence
    assert any("Unreliable transcription" in w for w in b.weaknesses)


def test_missing_word_timings_lower_confidence() -> None:
    timed = evidence_for(podcast())
    untimed = evidence_for(podcast(with_words=False))
    d_timed = generate_candidates(timed, CONFIG)[0]
    d_untimed = generate_candidates(untimed, CONFIG)[0]
    assert (
        score_draft(untimed, d_untimed, CONFIG).confidence
        < score_draft(timed, d_timed, CONFIG).confidence
    )


def test_duration_fit_prefers_the_target() -> None:
    evidence = evidence_for(podcast())
    config = CandidateConfig(min_s=15, target_s=40, max_s=60)
    near = CandidateDraft(
        evidence.sentences[0].start_s, evidence.sentences[10].end_s, 0, 10, (), ()
    )
    drafts = {n: score_draft(evidence, d, config) for n, d in {"near": near}.items()}
    value = next(c.value for c in drafts["near"].components if c.name == "duration_fit")
    assert 0.7 <= value <= 1.0
    tight = CandidateConfig(min_s=5, target_s=8, max_s=10)
    long_value = next(
        c.value for c in score_draft(evidence, near, tight).components if c.name == "duration_fit"
    )
    assert long_value < 0.5


def test_ranking_orders_dedups_and_limits(scored: list[ScoredCandidate]) -> None:
    kept, dropped = rank_candidates(scored, RankingConfig(max_count=5, max_overlap=0.5))
    assert 1 <= len(kept) <= 5
    assert len(kept) + dropped == len(scored)
    assert [c.score for c in kept] == sorted((c.score for c in kept), reverse=True)
    for i, a in enumerate(kept):
        for b in kept[i + 1 :]:
            assert overlap_fraction((a.start_s, a.end_s), (b.start_s, b.end_s)) <= 0.5
    loose, _ = rank_candidates(scored, RankingConfig(max_count=100, max_overlap=1.0))
    strict, _ = rank_candidates(scored, RankingConfig(max_count=100, max_overlap=0.1))
    assert len(loose) >= len(strict)


def test_ranking_does_not_depend_on_input_order(scored: list[ScoredCandidate]) -> None:
    kept_a, _ = rank_candidates(scored)
    kept_b, _ = rank_candidates(list(reversed(scored)))
    assert kept_a == kept_b


def test_feedback_nudges_scores_but_keeps_candidates(evidence: Evidence) -> None:
    draft = generate_candidates(evidence, CONFIG)[0]
    plain = score_draft(evidence, draft, CONFIG)
    span = (draft.start_s, draft.end_s)
    down = score_draft(evidence, draft, CONFIG, feedback=Feedback(rejected_spans=(span,)))
    up = score_draft(evidence, draft, CONFIG, feedback=Feedback(favorite_spans=(span,)))
    assert down.score < plain.score < up.score or plain.score >= 92  # may saturate at the top
    assert any("rejected" in w for w in down.weaknesses)
    assert any("favorited" in s for s in up.strengths)
    biased = score_draft(
        evidence, draft, CONFIG, feedback=Feedback(category_bias={plain.category: -1.0})
    )
    assert biased.score == pytest.approx(plain.score - 4.0, abs=0.01)


def test_category_bias_is_shrunk_for_small_samples() -> None:
    assert category_bias({"story": (1, 0), "humor": (0, 6), "advice": (0, 0)}) == {
        "advice": 0.0,
        "humor": -0.667,
        "story": 0.25,
    }


def test_candidate_key_is_stable_and_distinct() -> None:
    a = candidate_key("abc", 10.0, 40.0)
    assert a == candidate_key("abc", 10.0, 40.0)
    assert len(a) == 16
    assert a != candidate_key("abc", 10.0, 41.0)
    assert a != candidate_key("abd", 10.0, 40.0)


def test_overlap_fraction() -> None:
    assert overlap_fraction((0, 10), (5, 20)) == 0.5
    assert overlap_fraction((0, 10), (10, 20)) == 0.0
    assert overlap_fraction((0, 100), (10, 20)) == 1.0
    assert overlap_fraction((5, 5), (0, 10)) == 0.0


def test_tiny_and_sparse_transcripts_score_low_but_validly() -> None:
    evidence = evidence_for(script(["One line here now.", "And one more line."] * 2))
    drafts = generate_candidates(evidence, CONFIG)
    result = score_candidates(evidence, drafts, CONFIG)
    assert len(result) == 1
    assert any("Very little speech" in w for w in result[0].weaknesses)
    assert result[0].score < 60
    assert score_candidates(evidence_for(()), [], CONFIG) == []
