"""Caption matrix: synthetic transcripts with known timestamps, every preset (Phase 4C)."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest

from iftyedits.core.captions import MAX_CUE_S, CaptionCue, build_cues
from iftyedits.core.models import StoredSegment, TranscriptWord
from iftyedits.core.shorts import CAPTION_PRESETS, SAFE_BOTTOM, SAFE_TOP, CaptionStyle

Build = Callable[[], tuple[list[StoredSegment], float, float]]


def seg(
    pos: int,
    start: float,
    text: str,
    *,
    wps: float = 2.8,
    words: bool = True,
    end: float | None = None,
    conf: float = 0.95,
) -> StoredSegment:
    tokens = text.split()
    step = 1.0 / wps
    stop = end if end is not None else start + len(tokens) * step
    timed = (
        tuple(
            TranscriptWord(start + i * step, start + (i + 1) * step, t, conf)
            for i, t in enumerate(tokens)
        )
        if words
        else ()
    )
    return StoredSegment(pos + 1, 1, pos, start, stop, text, -0.1, 0.01, timed)


def short_sentences() -> tuple[list[StoredSegment], float, float]:
    return [seg(0, 0.0, "Hello there."), seg(1, 2.0, "This works.")], 0.0, 6.0


def long_sentence() -> tuple[list[StoredSegment], float, float]:
    text = "so what happened was that we walked into the room and everybody just stopped talking"
    return [seg(0, 0.0, text * 2)], 0.0, 20.0


def rapid_dialogue() -> tuple[list[StoredSegment], float, float]:
    lines = [seg(i, i * 1.2, "yes no maybe so right okay", wps=5.0) for i in range(6)]
    return lines, 0.0, 8.0


def with_silence() -> tuple[list[StoredSegment], float, float]:
    return [seg(0, 0.0, "before the long gap"), seg(1, 12.0, "after the long gap")], 0.0, 16.0


def long_word() -> tuple[list[StoredSegment], float, float]:
    word = "Pneumonoultramicroscopicsilicovolcanoconiosis"
    return [seg(0, 0.0, f"a {word} and {word}")], 0.0, 6.0


def unicode_text() -> tuple[list[StoredSegment], float, float]:
    return [seg(0, 0.0, "¿Qué pasó? 日本語のテキスト Ünïcödé — ok! 🙂")], 0.0, 8.0


def missing_word_times() -> tuple[list[StoredSegment], float, float]:
    return [seg(0, 1.0, "no word times here at all really", words=False, end=5.0)], 0.0, 8.0


def clip_boundary_words() -> tuple[list[StoredSegment], float, float]:
    # words span the clip start (3.0) and end (7.0); partly-outside words must not leak
    return [seg(0, 1.0, "one two three four five six seven eight nine ten", wps=1.5)], 3.0, 7.0


CASES: dict[str, Build] = {
    "short": short_sentences,
    "long": long_sentence,
    "rapid": rapid_dialogue,
    "silence": with_silence,
    "long_word": long_word,
    "unicode": unicode_text,
    "no_word_times": missing_word_times,
    "boundary": clip_boundary_words,
}


def cues_for(case: str, preset: str, **overrides: object) -> tuple[list[CaptionCue], float]:
    segments, start, end = CASES[case]()
    style = CaptionStyle.from_preset(preset, **overrides)
    return build_cues(segments, start, end, style), end - start


@pytest.mark.parametrize("preset", sorted(CAPTION_PRESETS))
@pytest.mark.parametrize("case", sorted(CASES))
def test_cues_are_ordered_non_overlapping_inside_the_clip_and_never_flash(
    case: str, preset: str
) -> None:
    cues, duration = cues_for(case, preset)
    assert cues, f"{case}/{preset} produced no captions"
    for cue in cues:
        assert 0.0 <= cue.start_s < cue.end_s <= duration + 1e-6
        assert cue.end_s - cue.start_s >= 0.05
        assert cue.end_s - cue.start_s <= MAX_CUE_S + 0.5  # nothing lingers on screen
        assert cue.text.strip()
        for w in cue.words:
            assert 0.0 <= w.start_s <= w.end_s <= duration + 1e-6
    for a, b in itertools.pairwise(cues):
        assert a.end_s <= b.start_s + 1e-6


@pytest.mark.parametrize("preset", sorted(CAPTION_PRESETS))
def test_no_cue_is_denser_than_the_line_budget(preset: str) -> None:
    style = CaptionStyle.from_preset(preset)
    budget = int(22 * style.max_lines * style.width_fraction / 0.86 * 64 / style.font_size)
    for case in ("long", "rapid", "short"):
        cues, _ = cues_for(case, preset)
        assert all(len(c.text) <= max(12, budget) + 12 for c in cues), (case, preset)


def test_silence_leaves_the_screen_empty() -> None:
    cues, _ = cues_for("silence", "classic")
    first, second = cues[0], cues[-1]
    assert second.start_s - first.end_s >= 5.0  # no caption bridges the 12 s gap


def test_a_long_word_is_kept_whole_never_cut_or_dropped() -> None:
    cues, _ = cues_for("long_word", "classic")
    joined = " ".join(c.text for c in cues)
    assert joined.count("Pneumonoultramicroscopicsilicovolcanoconiosis") == 2


def test_unicode_survives_and_nothing_is_invented() -> None:
    cues, _ = cues_for("unicode", "classic")
    joined = " ".join(c.text for c in cues)
    for fragment in ("¿Qué", "日本語のテキスト", "Ünïcödé", "🙂"):
        assert fragment in joined
    expected = ["¿Qué", "pasó?", "日本語のテキスト", "Ünïcödé", "—", "ok!", "🙂"]
    assert sorted(joined.split()) == sorted(expected)


def test_missing_word_times_are_marked_as_an_estimate_inside_the_segment() -> None:
    cues, _ = cues_for("no_word_times", "classic")
    assert all(c.timing in ("segment", "estimated") and not c.can_highlight for c in cues)
    assert cues[0].start_s == pytest.approx(1.0, abs=0.01)
    assert cues[-1].end_s == pytest.approx(5.0, abs=0.01)


def test_words_half_outside_the_clip_are_not_shown() -> None:
    cues, _ = cues_for("boundary", "classic")
    words = [w.text for c in cues for w in c.words]
    # words 1.0 + i/1.5: "three" ends at 3.0 (outside), "seven" starts at 5.0, "ten" at 7.0+
    assert "one" not in words and "two" not in words and "ten" not in words
    assert "five" in words


def test_low_confidence_words_keep_their_text() -> None:
    segments = [seg(0, 0.0, "mumbled words here", conf=0.05)]
    cues = build_cues(segments, 0.0, 5.0, CaptionStyle())
    assert " ".join(c.text for c in cues) == "mumbled words here"  # never rewritten or dropped


def test_corrupt_word_timing_falls_back_to_segment_timing() -> None:
    bad = StoredSegment(
        1,
        1,
        0,
        0.0,
        4.0,
        "alpha beta gamma",
        -0.1,
        0.01,
        (
            TranscriptWord(2.0, 2.5, "alpha", 0.9),
            TranscriptWord(0.5, 0.9, "beta", 0.9),  # goes backwards
            TranscriptWord(3.0, 3.4, "gamma", 0.9),
        ),
    )
    cues = build_cues([bad], 0.0, 5.0, CaptionStyle())
    assert cues and cues[0].timing in ("segment", "estimated")
    assert " ".join(c.text for c in cues) == "alpha beta gamma"


def test_overlapping_segments_never_overlap_on_screen() -> None:
    segments = [seg(0, 0.0, "first speaker talks over", wps=2.0), seg(1, 1.0, "second", wps=2.0)]
    cues = build_cues(segments, 0.0, 8.0, CaptionStyle())
    for a, b in itertools.pairwise(cues):
        assert a.end_s <= b.start_s + 1e-6


@pytest.mark.parametrize("position", [-5.0, 0.0, 0.5, 0.99, 7.0])
def test_every_position_lands_in_the_mobile_safe_area(position: float) -> None:
    style = CaptionStyle.from_preset("classic", position=position)
    assert SAFE_TOP <= style.position <= SAFE_BOTTOM


def _luminance(hex_color: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("preset", sorted(CAPTION_PRESETS))
def test_every_preset_is_readable(preset: str) -> None:
    style = CaptionStyle.from_preset(preset)
    backdrop = "#000000" if style.background == "box" else style.outline_color
    assert style.outline_px > 0 or style.background == "box"  # text always has a backing
    assert contrast(style.text_color, backdrop) >= 7.0
    if style.emphasis:
        assert contrast(style.emphasis_color, backdrop) >= 4.5
        assert contrast(style.emphasis_color, style.text_color) < 21  # not the same colour
        assert style.emphasis_color.upper() != style.text_color.upper()
    assert 28 <= style.font_size <= 140
