"""Caption emphasis and the Phase 4B presets."""

from __future__ import annotations

import pytest

from iftyedits.adapters.caption_render import render_cue
from iftyedits.core.captions import CaptionCue, CueWord, emphasis_indexes
from iftyedits.core.shorts import CAPTION_PRESETS, CaptionStyle, ShortConfig


def test_emphasis_is_a_fixed_rule_that_skips_common_words() -> None:
    words = ["I", "just", "made", "500", "dollars", "in", "the", "first", "hour"]
    picked = emphasis_indexes(words)
    assert picked == emphasis_indexes(list(words))  # deterministic
    assert 3 in picked  # the number
    assert all(words[i].lower() not in {"i", "the", "in", "just"} for i in picked)
    assert len(picked) <= 3 and len(picked) <= max(1, len(words) // 4)


def test_emphasis_handles_short_shouted_and_punctuated_text() -> None:
    assert emphasis_indexes(["Wow", "great"]) == frozenset()  # too short to stress anything
    assert emphasis_indexes([]) == frozenset()
    assert emphasis_indexes(["it", "was", "AMAZING", "honestly"]) == frozenset({2})
    assert emphasis_indexes(["and", "so", "on", "and", "so"]) == frozenset()
    assert emphasis_indexes(["we", "won", "the", "“championship!”", "finally"])
    assert emphasis_indexes(["très", "étonnant", "café", "100%", "réel"])
    assert emphasis_indexes(["…", "!!", "??", "—"]) == frozenset()


def _cue(text: str) -> CaptionCue:
    words = tuple(CueWord(w, i * 0.3, i * 0.3 + 0.3) for i, w in enumerate(text.split()))
    return CaptionCue(0.0, words[-1].end_s, text, words, "word", 0)


def _count(image: object, rgb: tuple[int, int, int]) -> int:
    from PIL import Image

    assert isinstance(image, Image.Image)
    pixels = image.load()
    assert pixels is not None
    return sum(
        1 for x in range(image.width) for y in range(image.height) if pixels[x, y] == (*rgb, 255)
    )


def test_emphasised_words_are_drawn_in_their_colour_in_the_render() -> None:
    cue = _cue("this took exactly 500 hours to finish")
    plain = CaptionStyle(text_color="#FFFFFF", emphasis_color="#FF0000", emphasis=False)
    stressed = CaptionStyle(text_color="#FFFFFF", emphasis_color="#FF0000", emphasis=True)
    base, _ = render_cue(cue, plain, (540, 960))
    marked, layout = render_cue(cue, stressed, (540, 960))
    assert _count(base, (255, 0, 0)) == 0
    assert _count(marked, (255, 0, 0)) > 50
    assert layout.inside_safe_area  # emphasis never changes the layout


def test_presets_are_valid_and_old_settings_keep_their_hash() -> None:
    for name in ("clean", "punchy", "cinematic", "mobile"):
        assert name in CAPTION_PRESETS
        CaptionStyle.from_preset(name).validate()
    assert CaptionStyle.from_preset("punchy").emphasis
    assert not CaptionStyle.from_preset("cinematic").emphasis
    plain = ShortConfig(start_s=0, end_s=20)
    assert "emphasis" not in plain.to_dict()["captions"]
    marked = ShortConfig(start_s=0, end_s=20, captions=CaptionStyle(emphasis=True))
    assert ShortConfig.from_json(marked.to_json()) == marked
    with pytest.raises(Exception, match="emphasis color"):
        CaptionStyle(emphasis_color="red").validate()
