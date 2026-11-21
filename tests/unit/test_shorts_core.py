"""Crop geometry, range validation, settings round-trip and caption cues (Phase 3)."""

from __future__ import annotations

import itertools

import pytest

from iftyedits.core.captions import build_cues, clean_text, timing_summary
from iftyedits.core.errors import ShortConfigError
from iftyedits.core.models import StoredSegment, TranscriptWord
from iftyedits.core.shorts import (
    AudioSettings,
    CaptionStyle,
    OutputSettings,
    PolishSettings,
    ShortConfig,
    compute_crop,
    validate_range,
)

# ----------------------------------------------------------------------- crop geometry


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ((1920, 1080), (606, 1078)),
        ((1280, 720), (404, 718)),
        ((3840, 2160), (1214, 2158)),
        ((1080, 1080), (606, 1078)),
        ((640, 480), (270, 480)),
    ],
)
def test_landscape_and_square_sources_crop_to_even_vertical_windows(
    src: tuple[int, int], expected: tuple[int, int]
) -> None:
    crop = compute_crop(*src)
    assert (crop.width, crop.height) == expected
    assert crop.width % 2 == 0 and crop.height % 2 == 0
    assert abs(crop.aspect_error) < 0.01


def test_1080p_landscape_rounding_is_documented_by_numbers() -> None:
    crop = compute_crop(1920, 1080)
    assert 1080 * 9 / 16 == 607.5
    assert crop.width == 606 and crop.x == 656 and crop.y == 0
    assert (crop.out_w, crop.out_h) == (1080, 1920)


def test_position_moves_the_window_and_is_clamped() -> None:
    left, mid, right = (compute_crop(1920, 1080, p).x for p in (0.0, 0.5, 1.0))
    assert (left, mid, right) == (0, 656, 1314)
    assert compute_crop(1920, 1080, -5).x == left
    assert compute_crop(1920, 1080, 9).x == right
    assert compute_crop(1920, 1080, float("nan")).x == mid
    for p in (0.0, 0.31, 0.5, 0.77, 1.0):
        crop = compute_crop(1920, 1080, p)
        assert crop.x >= 0 and crop.x + crop.width <= 1920
        assert crop.x % 2 == 0


def test_already_vertical_source_is_not_cropped() -> None:
    crop = compute_crop(1080, 1920)
    assert crop.is_full_frame
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1080, 1920)


def test_tall_non_916_portrait_is_trimmed_top_and_bottom_without_distortion() -> None:
    crop = compute_crop(1000, 2000)
    assert (crop.width, crop.height) == (1000, 1776)
    assert crop.y == 112 and crop.x == 0
    assert crop.width / crop.height == pytest.approx(9 / 16, abs=0.002)


def test_odd_source_sizes_stay_inside_the_frame() -> None:
    crop = compute_crop(1921, 1081)
    assert crop.x + crop.width <= 1921 and crop.y + crop.height <= 1081
    assert crop.width % 2 == 0 and crop.height % 2 == 0


@pytest.mark.parametrize("size", [(0, 1080), (1, 1), (1920, 0)])
def test_tiny_or_empty_frames_are_rejected(size: tuple[int, int]) -> None:
    with pytest.raises(ShortConfigError):
        compute_crop(*size)


def test_unknown_output_size_is_rejected() -> None:
    with pytest.raises(ShortConfigError):
        compute_crop(1920, 1080, out_size="4k")


def test_preview_and_export_share_the_source_window() -> None:
    full = compute_crop(1920, 1080, 0.3, out_size="1080p")
    draft = compute_crop(1920, 1080, 0.3, out_size="540p")
    assert (full.x, full.y, full.width, full.height) == (
        draft.x,
        draft.y,
        draft.width,
        draft.height,
    )
    assert (draft.out_w, draft.out_h) == (540, 960)


# ----------------------------------------------------------------------- ranges


def test_valid_range_is_rounded_to_milliseconds() -> None:
    assert validate_range(10.00049, 20.0, 60) == (10.0, 20.0)


@pytest.mark.parametrize(
    ("start", "end", "duration"),
    [
        (-1, 10, 60),
        (10, 10, 60),
        (20, 10, 60),
        (10, 11, 60),
        (0, 181, 600),
        (50, 70, 60),
        (float("nan"), 10, 60),
        (0, float("inf"), 60),
    ],
)
def test_bad_ranges_are_rejected(start: float, end: float, duration: float) -> None:
    with pytest.raises(ShortConfigError):
        validate_range(start, end, duration)


def test_range_ending_exactly_at_the_video_end_is_allowed() -> None:
    assert validate_range(50, 60, 60) == (50.0, 60.0)


# ----------------------------------------------------------------------- settings


def test_config_round_trips_through_json_with_overrides() -> None:
    config = ShortConfig(
        start_s=10,
        end_s=40,
        crop_position=0.25,
        captions=CaptionStyle(preset="bold", highlight=True, font_size=72),
        text_overrides={3: "Corrected text ünï 日本"},
        audio=AudioSettings(volume_percent=120, fade_out_s=1.0, normalize=True),
        polish=PolishSettings(fade_in_s=0.5, brightness=0.05),
        output=OutputSettings(size="720p", quality="balanced"),
    ).validate(100)
    assert ShortConfig.from_json(config.to_json()) == config


def test_config_hash_changes_with_every_relevant_input() -> None:
    base = ShortConfig(start_s=10, end_s=40)
    ref = base.config_hash(video_sha256="a" * 64, kind="export", has_audio=True)
    from dataclasses import replace

    assert ref == base.config_hash(video_sha256="a" * 64, kind="export", has_audio=True)
    others = [
        replace(base, end_s=41).config_hash(video_sha256="a" * 64, kind="export", has_audio=True),
        replace(base, crop_position=0.4).config_hash(
            video_sha256="a" * 64, kind="export", has_audio=True
        ),
        replace(base, text_overrides={1: "x"}).config_hash(
            video_sha256="a" * 64, kind="export", has_audio=True
        ),
        base.config_hash(video_sha256="b" * 64, kind="export", has_audio=True),
        base.config_hash(video_sha256="a" * 64, kind="preview", has_audio=True),
        base.config_hash(video_sha256="a" * 64, kind="export", has_audio=False),
    ]
    assert len({ref, *others}) == 7


@pytest.mark.parametrize(
    "bad",
    [
        CaptionStyle(font_size=5),
        CaptionStyle(text_color="red"),
        AudioSettings(volume_percent=500),
        AudioSettings(fade_in_s=-1),
        PolishSettings(contrast=5),
        PolishSettings(fade_in_s=9),
        OutputSettings(size="8k"),
        OutputSettings(quality="ultra"),
    ],
)
def test_out_of_range_settings_are_rejected(bad: object) -> None:
    with pytest.raises(ShortConfigError):
        bad.validate()  # type: ignore[attr-defined]


def test_fades_longer_than_the_clip_are_rejected() -> None:
    with pytest.raises(ShortConfigError):
        ShortConfig(start_s=0, end_s=4, audio=AudioSettings(fade_in_s=3, fade_out_s=3)).validate()


def test_unreadable_stored_settings_give_a_clear_error() -> None:
    with pytest.raises(ShortConfigError):
        ShortConfig.from_json("{not json")
    with pytest.raises(ShortConfigError):
        ShortConfig.from_json('{"start_s": "abc", "end_s": 5}')


def test_corrections_with_control_characters_are_rejected() -> None:
    with pytest.raises(ShortConfigError):
        ShortConfig(start_s=0, end_s=10, text_overrides={0: "a\x00b"}).validate()


# ----------------------------------------------------------------------- captions


def _seg(
    position: int, start: float, end: float, text: str, *, words: bool = True
) -> StoredSegment:
    tokens = text.split()
    step = (end - start) / max(len(tokens), 1)
    timed = tuple(
        TranscriptWord(start + i * step, start + (i + 1) * step, t, 0.95)
        for i, t in enumerate(tokens)
    )
    return StoredSegment(
        position + 1, 1, position, start, end, text, -0.1, 0.01, timed if words else ()
    )


def test_cue_times_are_clip_relative_and_inside_the_clip() -> None:
    segments = [
        _seg(0, 8.0, 12.0, "before and across the start"),
        _seg(1, 12.5, 15.0, "inside the clip now"),
    ]
    cues = build_cues(segments, 10.0, 20.0, CaptionStyle())
    assert cues
    assert min(c.start_s for c in cues) >= 0.0
    assert max(c.end_s for c in cues) <= 10.0
    assert cues[-1].start_s == pytest.approx(2.5, abs=0.01)
    assert "before" not in " ".join(c.text for c in cues)


def test_segments_outside_the_range_are_ignored() -> None:
    cues = build_cues(
        [_seg(0, 0, 5, "too early"), _seg(1, 50, 55, "too late")], 10, 40, CaptionStyle()
    )
    assert cues == []
    assert "No speech" in timing_summary(cues)


def test_word_timing_is_used_when_present_and_segment_timing_when_not() -> None:
    cues = build_cues([_seg(0, 10, 13, "three little words")], 10, 20, CaptionStyle())
    assert cues[0].timing == "word" and cues[0].can_highlight
    cues = build_cues([_seg(0, 10, 13, "three little words", words=False)], 10, 20, CaptionStyle())
    assert cues[0].timing == "segment" and not cues[0].can_highlight
    assert cues[0].start_s == pytest.approx(0.0) and cues[0].end_s == pytest.approx(3.0)


def test_corrected_text_replaces_transcript_text_without_word_timing() -> None:
    segment = _seg(4, 10, 14, "teh wrong text")
    cues = build_cues([segment], 10, 20, CaptionStyle(), {4: "The right text"})
    assert cues[0].text == "The right text" and cues[0].timing == "estimated"
    assert segment.text == "teh wrong text"  # the stored transcript is untouched


def test_long_segments_are_split_into_readable_cues() -> None:
    text = " ".join(f"word{i}" for i in range(40))
    cues = build_cues([_seg(0, 10, 26, text)], 10, 30, CaptionStyle())
    assert len(cues) > 2
    assert all(c.end_s > c.start_s for c in cues)
    assert all(a.end_s <= b.start_s + 1e-6 for a, b in itertools.pairwise(cues))


def test_unicode_and_punctuation_are_preserved() -> None:
    cues = build_cues([_seg(0, 10, 14, "¿Qué pasó? Ünï — 日本語!")], 10, 20, CaptionStyle())
    assert "¿Qué pasó?" in cues[0].text and "日本語!" in cues[0].text


def test_clean_text_removes_control_characters_and_collapses_spaces() -> None:
    assert clean_text("  a\x00b \t c\x07  ") == "ab c"


def test_caption_position_is_clamped_into_the_safe_area() -> None:
    from iftyedits.core.shorts import SAFE_BOTTOM, SAFE_TOP

    assert CaptionStyle(position=2.0).validate().position == SAFE_BOTTOM
    assert CaptionStyle(position=-1.0).validate().position == SAFE_TOP
    with pytest.raises(ShortConfigError):
        CaptionStyle(position=float("nan")).validate()


def test_ui_labels_cover_every_preset_size_and_quality() -> None:
    from iftyedits.core.shorts import CAPTION_PRESETS, OUTPUT_SIZES, QUALITY_CRF
    from iftyedits.ui.shorts import CAPTION_PRESET_LABELS, QUALITY_LABELS, SIZE_LABELS

    assert set(CAPTION_PRESET_LABELS) == set(CAPTION_PRESETS)
    assert set(SIZE_LABELS) == set(OUTPUT_SIZES)
    assert set(QUALITY_LABELS) == set(QUALITY_CRF)
