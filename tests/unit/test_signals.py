"""Phase 2A content signals: sentences, transcript, audio and scene signals (pure logic)."""

from __future__ import annotations

import math

import pytest

from iftyedits.core.clip_models import (
    LoudnessEnvelope,
    SceneCut,
    SignalKind,
    SignalSource,
)
from iftyedits.core.models import TranscriptSegment, TranscriptWord
from iftyedits.core.signals import (
    audio_signals,
    build_evidence,
    build_sentences,
    hook_strength,
    lexicon_supported,
    match_lexicon,
    question_signals,
    rapid_exchange_signals,
    scene_signals,
    silence_threshold,
    starts_with_connector,
    topic_shift_signals,
    transcript_signals,
    wording_signals,
)
from tests.clip_fakes import PODCAST, line_segment, podcast, script


def kinds(signals: object) -> set[SignalKind]:
    return {s.kind for s in signals}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- sentences


def test_sentences_are_rebuilt_across_segment_boundaries() -> None:
    words_a = (TranscriptWord(0.0, 0.4, " So"), TranscriptWord(0.4, 0.8, " the"))
    words_b = (TranscriptWord(0.9, 1.3, " trailer"), TranscriptWord(1.3, 1.8, " starts."))
    segments = [
        TranscriptSegment(0.9, 1.8, " trailer starts.", words_b),
        TranscriptSegment(0.0, 0.8, " So the", words_a),  # out of order on purpose
    ]
    sentences = build_sentences(segments)
    assert len(sentences) == 1
    assert sentences[0].text == "So the trailer starts."
    assert (sentences[0].start_s, sentences[0].end_s) == (0.0, 1.8)
    assert sentences[0].terminal and sentences[0].timed_by_words


def test_long_pause_splits_a_sentence_without_punctuation() -> None:
    segments = [line_segment(0.0, "first part of a thought"), line_segment(5.0, "second part")]
    sentences = build_sentences(segments)
    assert [s.text for s in sentences] == ["first part of a thought", "second part"]
    assert not sentences[0].terminal
    assert sentences[0].gap_after_s == pytest.approx(5.0 - sentences[0].end_s, abs=1e-3)
    assert sentences[1].gap_after_s == 999.0


def test_segments_without_word_timings_are_split_and_interpolated() -> None:
    seg = TranscriptSegment(10.0, 20.0, " One sentence here. Another one!", (), -0.5, 0.1)
    sentences = build_sentences([seg])
    assert [s.text for s in sentences] == ["One sentence here.", "Another one!"]
    assert sentences[0].start_s == 10.0 and sentences[-1].end_s == pytest.approx(20.0)
    assert not any(s.timed_by_words for s in sentences)
    assert sentences[0].confidence == pytest.approx(math.exp(-0.5), abs=1e-3)


def test_confidence_comes_from_word_probabilities() -> None:
    seg = line_segment(0.0, "a b c.", probability=0.5)
    assert build_sentences([seg])[0].confidence == pytest.approx(0.5)


def test_very_long_runs_are_cut_into_bounded_sentences() -> None:
    text = " ".join(["word"] * 200)  # no punctuation, no pauses
    sentences = build_sentences([line_segment(0.0, text, wps=3.0)])
    assert len(sentences) >= 2
    assert all(s.duration_s <= 45.0 + 0.5 for s in sentences)


def test_empty_and_blank_input() -> None:
    assert build_sentences([]) == ()
    assert build_sentences([TranscriptSegment(0.0, 1.0, "   ")]) == ()


# ---------------------------------------------------------------------- transcript signals


def test_questions_and_question_answer_exchanges() -> None:
    sentences = build_sentences(script(PODCAST[1:5]))
    found = question_signals(sentences)
    assert SignalKind.QUESTION in kinds(found)
    qa = [s for s in found if s.kind is SignalKind.QUESTION_ANSWER]
    assert len(qa) == 1
    assert qa[0].start_s == sentences[0].start_s
    assert qa[0].end_s == sentences[-1].end_s
    assert qa[0].source is SignalSource.TRANSCRIPT
    assert "grow your channel" in qa[0].evidence["question"]


def test_question_without_answer_is_not_an_exchange() -> None:
    sentences = build_sentences(script(["Why would anyone do that?"]))
    assert kinds(question_signals(sentences)) == {SignalKind.QUESTION}


def test_answer_stops_at_a_long_gap() -> None:
    sentences = build_sentences(
        script(["What happened next?", "We left early.", "Much later something else."],
               pauses={1: 6.0})
    )  # fmt: skip
    found = [s for s in question_signals(sentences) if s.kind is SignalKind.QUESTION_ANSWER]
    assert found == []  # one short answer sentence is not enough


def test_hook_detection_and_connector_starts() -> None:
    hook = build_sentences(script(["Here's the craziest thing that happened today."]))[0]
    weak = build_sentences(script(["And then we went home after that."]))[0]
    assert hook_strength(hook, True) is not None
    assert hook_strength(weak, True) is None
    assert starts_with_connector(weak.text)
    listy = build_sentences(script(["Three things every creator gets wrong."]))[0]
    assert hook_strength(listy, True) == (0.8, "announces a list")


def test_wording_cues_record_matched_words() -> None:
    sentences = build_sentences(script(PODCAST[4:10]))
    found = wording_signals(sentences)
    assert {SignalKind.STORY, SignalKind.EMOTIONAL_WORDING, SignalKind.HUMOR_CUE} <= kinds(found)
    story = next(s for s in found if s.kind is SignalKind.STORY)
    assert "i remember" in story.evidence["matched"]
    assert 0 < story.confidence < 0.92  # wording cues are scaled down
    assert match_lexicon("Absolutely unbelievable!", (("unbelievable", 1.0),)) == [
        ("unbelievable", 1.0)
    ]
    assert match_lexicon("believable", (("unbelievable", 1.0),)) == []


def test_laughter_marks_count_as_humor() -> None:
    sentences = build_sentences(script(["(laughs) okay fine."]))
    assert SignalKind.HUMOR_CUE in kinds(wording_signals(sentences))


def test_rapid_exchange_needs_short_lively_lines() -> None:
    sentences = build_sentences(script(PODCAST[21:26], gap=0.3))
    found = rapid_exchange_signals(sentences)
    assert len(found) == 1
    assert "speakers are not identified" in found[0].label
    calm = build_sentences(script(["Fine.", "Okay.", "Sure.", "Right."]))
    assert rapid_exchange_signals(calm) == []


def test_topic_shift_is_found_between_unrelated_topics() -> None:
    cooking = [
        f"Add the {w} to the pan with garlic butter and onions, stir the sauce gently."
        for w in ("tomatoes", "pepper", "basil", "pasta", "cheese", "spinach", "mushrooms")
    ]
    rockets = [
        f"The {w} engine burns liquid oxygen while the booster rocket climbs into orbit."
        for w in ("first", "second", "third", "upper", "main", "backup", "final")
    ]
    sentences = build_sentences(script(cooking + rockets, gap=0.3))
    shifts = topic_shift_signals(sentences)
    assert len(shifts) == 1
    boundary = sentences[len(cooking) - 1].end_s
    assert shifts[0].start_s == pytest.approx(boundary, abs=0.01)
    assert shifts[0].evidence["similarity"] < shifts[0].evidence["typical_similarity"]


def test_pace_pause_and_low_confidence_signals() -> None:
    lines = ["one two three four five six seven."] * 6
    segments = list(script(lines, wps=2.5, pauses={2: 2.5}))
    segments.append(line_segment(60.0, " ".join(["fast"] * 24) + ".", wps=6.0))
    segments.append(line_segment(70.0, "mumbled words here.", probability=0.2))
    found = transcript_signals(build_sentences(segments), lexicon_supported=False)
    assert {SignalKind.FAST_PACE, SignalKind.PAUSE, SignalKind.LOW_CONFIDENCE} <= kinds(found)
    pause = next(s for s in found if s.kind is SignalKind.PAUSE)
    assert pause.evidence["gap_s"] == pytest.approx(2.5, abs=0.05)


def test_non_english_transcripts_skip_wording_cues() -> None:
    sentences = build_sentences(script(PODCAST))
    assert lexicon_supported("en", sentences)
    assert not lexicon_supported("de", sentences)
    assert lexicon_supported(None, sentences)  # unknown language, ASCII text
    assert not lexicon_supported(None, build_sentences(script(["Привет как дела сегодня."])))
    found = transcript_signals(sentences, lexicon_supported=False)
    assert not kinds(found) & {SignalKind.STORY, SignalKind.HUMOR_CUE, SignalKind.INSIGHT}
    assert SignalKind.QUESTION in kinds(found)


# ---------------------------------------------------------------------- audio and video


def envelope(*sections: tuple[float, float]) -> LoudnessEnvelope:
    """Envelope from ``(seconds, dBFS)`` sections at 0.1 s frames."""
    db: list[float] = []
    for seconds, level in sections:
        db += [level] * round(seconds / 0.1)
    return LoudnessEnvelope(0.1, tuple(db))


def test_silence_and_volume_rise() -> None:
    env = envelope((10, -25), (2, -70), (6, -25), (1, -8), (5, -25))
    found = audio_signals(env)
    silence = [s for s in found if s.kind is SignalKind.SILENCE]
    peaks = [s for s in found if s.kind is SignalKind.LOUDNESS_PEAK]
    assert len(silence) == 1
    assert silence[0].start_s == pytest.approx(10.0) and silence[0].end_s == pytest.approx(12.0)
    assert len(peaks) == 1
    assert peaks[0].start_s == pytest.approx(18.0, abs=0.5)
    assert peaks[0].evidence["rise_db"] >= 10
    assert peaks[0].source is SignalSource.AUDIO


def test_short_dips_are_not_silence_and_steady_audio_has_no_peaks() -> None:
    assert audio_signals(envelope((10, -25), (0.5, -80), (10, -25))) == []


def test_quiet_recordings_use_a_lower_silence_threshold() -> None:
    quiet = envelope((20, -40))
    assert silence_threshold(quiet) == pytest.approx(-60.0)
    assert audio_signals(quiet) == []
    assert audio_signals(LoudnessEnvelope(0.1, ())) == []


def test_scene_signals_are_video_points() -> None:
    found = scene_signals([SceneCut(3.0, 0.7)])
    assert found[0].source is SignalSource.VIDEO
    assert found[0].start_s == found[0].end_s == 3.0
    assert found[0].strength == pytest.approx(0.7)


# ---------------------------------------------------------------------- timeline


def test_evidence_is_sorted_and_reproducible() -> None:
    kwargs = {
        "duration_s": 240.0,
        "language": "en",
        "loudness": envelope((60, -25), (1, -6), (179, -25)),
        "scene_cuts": [SceneCut(30.0, 0.5)],
    }
    first = build_evidence(podcast(), **kwargs)  # type: ignore[arg-type]
    second = build_evidence(podcast(), **kwargs)  # type: ignore[arg-type]
    assert first == second
    starts = [(s.start_s, s.end_s, s.kind.value) for s in first.signals]
    assert starts == sorted(starts)
    sources = {s.source for s in first.signals}
    assert sources == {SignalSource.TRANSCRIPT, SignalSource.AUDIO, SignalSource.VIDEO}
    assert first.lexicon_supported


def test_evidence_without_speech_audio_or_video() -> None:
    evidence = build_evidence([], duration_s=30.0, language=None, loudness=None, scene_cuts=None)
    assert evidence.sentences == () and evidence.signals == ()
    assert evidence.scene_cuts is None and evidence.loudness is None
    assert not evidence.lexicon_supported
