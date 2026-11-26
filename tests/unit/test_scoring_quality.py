"""Scoring quality (scoring v2): boilerplate, emotion precision, strengths vs weaknesses."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from iftyedits.core.candidates import CandidateConfig, generate_candidates
from iftyedits.core.clip_models import CandidateDraft, Evidence, ScoredCandidate, SignalKind
from iftyedits.core.models import TranscriptSegment
from iftyedits.core.scoring import SCORING_VERSION, score_candidates, score_draft
from iftyedits.core.signals import (
    SIGNALS_VERSION,
    boilerplate_match,
    boilerplate_share,
    build_evidence,
    build_sentences,
    hook_signals,
    hook_strength,
    is_negated,
    match_lexicon,
    question_signals,
    rapid_exchange_signals,
    wording_signals,
)
from tests.clip_fakes import script

CONFIG = CandidateConfig()

CONTENT = (
    "Nobody expected the studio to reveal the whole map in one trailer.",
    "The first thing you notice is how dense the streets are around the harbour.",
    "Cars weave between pedestrians and the crowds react to every siren nearby.",
    "Then a chase starts and the camera pulls back over the whole bridge at once.",
    "Turns out the police helicopters actually track you across several districts.",
    "That detail alone changes how every mission could be planned from now on.",
    "The rain effects on the road also look far more physical than before.",
    "Honestly the lighting at dusk is the most impressive part of the footage.",
)
OUTRO = (
    "If you liked this video, click like and subscribe to the channel.",
    "Hit the bell icon so you never miss a video.",
    "Thanks for watching and I will see you in the next one.",
)


def evidence_for(lines: Sequence[str], **kwargs: object) -> Evidence:
    segments: tuple[TranscriptSegment, ...] = script(lines, **kwargs)  # type: ignore[arg-type]
    return build_evidence(
        segments, duration_s=segments[-1].end_s + 2.0, language="en", loudness=None, scene_cuts=None
    )


def whole(evidence: Evidence) -> CandidateDraft:
    sentences = evidence.sentences
    return CandidateDraft(
        sentences[0].start_s,
        sentences[-1].end_s + 0.2,
        0,
        len(sentences) - 1,
        ("test",),
        (),
    )


def sentence(text: str) -> object:
    return build_sentences(script([text]))[0]


# ----------------------------------------------------------------------------- A. boilerplate


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("If you liked this video, click like and subscribe.", "call_to_action"),
        ("Don't forget to hit the bell icon.", "call_to_action"),
        ("Link in the description below.", "call_to_action"),
        ("Hey, folks, it's Falcon.", "intro"),
        ("Welcome back to the channel everyone.", "intro"),
        ("In today's video we look at the new trailer.", "intro"),
        ("Thanks for watching and see you in the next one.", "outro"),
        ("Thank you very much for watching this video.", "outro"),
        ("Leave us a comment and enable notifications.", "call_to_action"),
        ("We will have brand new videos every day of the week.", "call_to_action"),
        ("That's all for today, until next time.", "outro"),
    ],
)
def test_boilerplate_is_recognised(text: str, kind: str) -> None:
    found = boilerplate_match(build_sentences(script([text]))[0])
    assert found is not None and found.kind == kind and found.phrases


@pytest.mark.parametrize(
    "text",
    [
        "The studio now has a million subscribers on the platform.",  # a word, not a prompt
        "You should check out the harbour in the second district.",  # one weak cue
        "Welcome to the most dangerous district in the game.",  # one weak cue
        "We saw the cars and the rain and the harbour and the helicopters and the crowds "
        "and the bridge and then they said to subscribe but nobody really cared about it at all.",
        "Nobody expected the studio to reveal the whole map in one trailer.",
    ],
)
def test_ordinary_sentences_are_not_boilerplate(text: str) -> None:
    assert boilerplate_match(build_sentences(script([text]))[0]) is None


def test_cta_is_never_a_hook_or_a_question() -> None:
    cta = build_sentences(script(["If you liked this video, click like."]))[0]
    assert hook_strength(cta, True) is None
    sentences = build_sentences(script(["Hey guys what's up?", "If you liked this video, like."]))
    assert hook_signals(sentences, True) == []
    assert question_signals(sentences) == []
    real = build_sentences(script(["If you have played the first game, you know the drill."]))[0]
    assert hook_strength(real, True) is not None  # a real "if you" line still hooks


def test_cta_wording_is_not_emotion() -> None:
    sentences = build_sentences(script(["If you love this video, please subscribe!"]))
    assert wording_signals(sentences) == []


def test_rapid_cta_lines_are_not_a_dialogue() -> None:
    lines = ["Like!", "Subscribe!", "Hit the bell!", "Thanks for watching!", "Bye bye!"]
    assert rapid_exchange_signals(build_sentences(script(lines, gap=0.2))) == []


def test_boilerplate_share_is_word_weighted() -> None:
    sentences = build_sentences(script([*CONTENT, OUTRO[0]]))
    share = boilerplate_share(sentences)
    assert 0.05 < share < 0.3
    assert boilerplate_share(build_sentences(script(CONTENT))) == 0.0
    assert boilerplate_share(build_sentences(script(OUTRO))) > 0.85
    assert boilerplate_share(()) == 0.0


def test_a_pure_outro_is_penalised_heavily_and_says_why() -> None:
    evidence = evidence_for([*OUTRO, *OUTRO])
    result = score_draft(evidence, whole(evidence), CONFIG)
    info = result.evidence["boilerplate"]
    assert info["share"] > 0.85 and info["points"] >= 18
    assert info["sentences"] and info["sentences"][0]["phrases"]
    assert any(w.startswith("Channel boilerplate") for w in result.weaknesses)
    assert result.score < 50
    assert not any("boilerplate" in s.lower() for s in result.strengths)


def test_an_intro_clip_is_penalised_and_its_opening_is_not_a_hook() -> None:
    lines = [
        "Hey folks, it's Falcon and welcome back to the channel.",
        "In today's video we look at the brand new trailer.",
        "Before we get started, let's talk about what you can expect.",
        "So let's jump right in.",
    ]
    evidence = evidence_for(lines)
    result = score_draft(evidence, whole(evidence), CONFIG)
    assert result.evidence["boilerplate"]["share"] > 0.7
    parts = {c.name: c for c in result.components}
    assert parts["hook"].value < 0.3
    assert "boilerplate" in parts["hook"].detail
    assert any("opens on channel boilerplate" in w for w in result.weaknesses)


def test_a_clip_that_is_mostly_content_loses_little_to_one_cta_line() -> None:
    plain = evidence_for(CONTENT)
    with_cta = evidence_for([*CONTENT[:6], "Like and subscribe if you want more.", *CONTENT[6:]])
    clean = score_draft(plain, whole(plain), CONFIG)
    mixed = score_draft(with_cta, whole(with_cta), CONFIG)
    assert mixed.evidence["boilerplate"]["points"] <= 7
    assert clean.score - mixed.score < 9
    assert mixed.score > score_draft(*_outro_only()).score + 15


def _outro_only() -> tuple[Evidence, CandidateDraft, CandidateConfig]:
    evidence = evidence_for([*OUTRO, *OUTRO])
    return evidence, whole(evidence), CONFIG


def test_penalty_grows_with_the_boilerplate_share() -> None:
    points = []
    for tail in (0, 1, 2, 3):
        evidence = evidence_for([*CONTENT[:6], *OUTRO[:tail]])
        points.append(
            score_draft(evidence, whole(evidence), CONFIG).evidence["boilerplate"]["points"]
        )
    assert points == sorted(points) and points[0] == 0.0 and points[-1] > points[1] > 0


def test_a_genuine_hook_survives_incidental_boilerplate() -> None:
    lines = [
        "Here's the craziest thing that happened in the whole trailer.",
        *CONTENT[1:5],
        "Subscribe for more.",
        *CONTENT[5:],
    ]
    evidence = evidence_for(lines)
    result = score_draft(evidence, whole(evidence), CONFIG)
    parts = {c.name: c for c in result.components}
    assert parts["hook"].value >= 0.75
    assert any(s.startswith("Hook strength") for s in result.strengths)
    assert result.evidence["boilerplate"]["share"] < 0.15
    assert result.evidence["boilerplate"]["points"] < 7


def test_generation_avoids_starting_or_ending_on_boilerplate() -> None:
    lines = [
        "Hey folks, it's Falcon and welcome back to the channel.",
        *CONTENT,
        *CONTENT,
        *OUTRO,
    ]
    evidence = evidence_for(lines)
    drafts = generate_candidates(evidence, CONFIG)
    assert drafts
    flagged = {s.index for s in evidence.sentences if boilerplate_match(s) is not None}
    assert 0 in flagged and flagged >= {len(evidence.sentences) - 1}
    assert not any(d.first_sentence in flagged for d in drafts)
    kept = [d for d in drafts if d.last_sentence in flagged]
    assert len(kept) < len(drafts)


# ------------------------------------------------------------------- B. emotion and humor


@pytest.mark.parametrize(
    "text",
    [
        "We see R2 to hold hands, which is a funny game mechanic.",
        "This is let your love flow by the Bellamy Brothers playing in the background.",
        "We're going to see crazy zany NPCs you can interact with around the whole city.",
        "We see this weird disgusting prairie burger ad that just seems really over the top.",
        "That was not funny at all.",
        "It isn't scary and it is not disgusting either.",
        "The graphics are amazing and the draw distance is far beyond the last game.",
    ],
)
def test_ordinary_commentary_is_not_funny_or_emotional(text: str) -> None:
    found = wording_signals(build_sentences(script([text])))
    assert not {s.kind for s in found} & {SignalKind.EMOTIONAL_WORDING, SignalKind.HUMOR_CUE}


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Oh my god, the shooting folks.", SignalKind.EMOTIONAL_WORDING),
        ("I was absolutely terrified.", SignalKind.EMOTIONAL_WORDING),
        ("That's crazy.", SignalKind.EMOTIONAL_WORDING),  # short reaction
        ("This is insane and absolutely incredible!", SignalKind.EMOTIONAL_WORDING),
        ("That was hilarious.", SignalKind.HUMOR_CUE),
        ("(laughs) okay fine.", SignalKind.HUMOR_CUE),
        ("It was so funny!", SignalKind.HUMOR_CUE),
    ],
)
def test_real_reactions_are_still_found(text: str, kind: SignalKind) -> None:
    found = wording_signals(build_sentences(script([text])))
    assert kind in {s.kind for s in found}


def test_borderline_a_weak_cue_needs_support() -> None:
    weak = "The map has a lot of crazy things to find when you explore the world for long."
    assert not wording_signals(build_sentences(script([weak])))
    assert wording_signals(build_sentences(script([weak.replace("long.", "long!")])))
    two = "The map is crazy and the missions are honestly incredible when you explore it."
    assert SignalKind.EMOTIONAL_WORDING in {
        s.kind for s in wording_signals(build_sentences(script([two])))
    }


def test_negation_and_literal_phrases() -> None:
    assert is_negated("that was not funny", "funny")
    assert not is_negated("that was funny, not sad", "funny")
    assert match_lexicon("guess what happened next", (("guess what", 0.9),))
    assert not match_lexicon("i guess what matters is time", (("guess what", 0.9),))


def test_one_mild_word_does_not_make_a_clip_funny_or_emotional() -> None:
    lines = [*CONTENT[:4], "That's crazy.", *CONTENT[4:], *CONTENT[:3]]
    evidence = evidence_for(lines)
    assert any(s.kind is SignalKind.EMOTIONAL_WORDING for s in evidence.signals)
    result = score_draft(evidence, whole(evidence), CONFIG)
    assert result.category not in {"emotional", "humor"}


def test_strong_evidence_still_labels_a_clip_emotional() -> None:
    lines = [*CONTENT[1:4], "Oh my god, I was terrified!", *CONTENT[5:]]
    evidence = evidence_for(lines)
    result = score_draft(evidence, whole(evidence), CONFIG)
    assert result.category == "emotional"


# ------------------------------------------------- C. strengths and weaknesses are separate

NEGATIVE_WORDS = ("Weak start", "mid-thought", "unanswered", "so the clip opens with setup")


def test_no_negative_evidence_under_strengths() -> None:
    lines = [*CONTENT, *CONTENT, *OUTRO]
    evidence = evidence_for(lines)
    scored = score_candidates(evidence, generate_candidates(evidence, CONFIG), CONFIG)
    assert scored
    for c in scored:
        for text in c.strengths:
            assert not any(word in text for word in NEGATIVE_WORDS), text
            assert "pronoun" not in text and "continuation word" not in text


def test_a_high_component_with_a_flaw_lists_the_flaw_as_a_weakness() -> None:
    lines = ["They tried to copy other people instead of finding their own voice.", *CONTENT[1:7]]
    evidence = evidence_for(lines)
    result = score_draft(evidence, whole(evidence), CONFIG)
    context = next(c for c in result.components if c.name == "context")
    assert context.value >= 0.7 and "Weak start" in context.detail
    assert any(
        w == "Context completeness: opens with a pronoun that points back"
        for w in result.weaknesses
    )
    assert not any(
        s.startswith("Context completeness") and "pronoun" in s for s in result.strengths
    )
    assert any(s.startswith("Context completeness: ") for s in result.strengths)


def test_missing_cues_are_not_listed_as_weaknesses() -> None:
    evidence = evidence_for(CONTENT)
    result = score_draft(evidence, whole(evidence), CONFIG)
    assert not any("No matching cue found" in w for w in result.weaknesses)


def test_penalties_come_first_in_the_weaknesses() -> None:
    evidence = evidence_for([*OUTRO, *OUTRO])
    result = score_draft(evidence, whole(evidence), CONFIG)
    assert result.weaknesses[0].startswith("Channel boilerplate")
    assert result.explanation.count("Strengths:") <= 1


# --------------------------------------------------------------------------- D. versions


def test_versions_are_bumped_and_recorded() -> None:
    assert SCORING_VERSION == 3 and SIGNALS_VERSION == 2
    evidence = evidence_for(CONTENT)
    result: ScoredCandidate = score_draft(evidence, whole(evidence), CONFIG)
    assert result.evidence["scoring_version"] == 3
    assert result == score_draft(evidence, whole(evidence), CONFIG)  # deterministic


FILLER = (
    "Um so yeah you know it is like what it is and stuff.",
    "Yeah so I mean it was kind of like you know whatever.",
    "And then it was um like yeah so that is that I guess.",
    "So yeah you know like I do not know it is just like that.",
)


def _density(lines: Sequence[str]) -> float:
    evidence = evidence_for(lines)
    result = score_draft(evidence, whole(evidence), CONFIG)
    return next(c.value for c in result.components if c.name == "density")


def test_density_separates_substance_from_filler() -> None:
    assert _density(CONTENT) > _density(FILLER) + 0.3


def test_density_is_deterministic_and_listed_in_the_breakdown() -> None:
    evidence = evidence_for(CONTENT)
    a = score_draft(evidence, whole(evidence), CONFIG)
    b = score_draft(evidence, whole(evidence), CONFIG)
    assert a.score == b.score and [c.value for c in a.components] == [c.value for c in b.components]
    assert "density" in [c.name for c in a.components]
