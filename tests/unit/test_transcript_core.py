"""Transcript normalisation, language validation and subtitle exports (pure functions)."""

from __future__ import annotations

import json
import math
import re

import pytest

from iftyedits.core.models import StoredSegment, Transcript, TranscriptSegment, TranscriptWord
from iftyedits.core.transcript import clean_text, normalize_segments, validate_language
from iftyedits.services.subtitles import export, format_timestamp, to_srt, to_text, to_vtt
from tests.transcription_fakes import segment

SRT_TIME = r"\d{2}:\d{2}:\d{2},\d{3}"
VTT_TIME = r"\d{2}:\d{2}:\d{2}\.\d{3}"


# ------------------------------------------------------------------ normalisation


def test_clean_text_collapses_whitespace() -> None:
    assert clean_text("  Hello \n\n  world\t ") == "Hello world"


def test_normalize_orders_strips_and_rounds() -> None:
    raw = [
        segment(2.00049, 3.0, "  second  "),
        segment(0.0, 1.23456, " first "),
    ]
    result = normalize_segments(raw, 10.0)
    assert [s.text for s in result.segments] == ["first", "second"]
    assert result.segments[0].end_s == 1.235
    assert result.segments[1].start_s == 2.0
    assert result.dropped == 0


def test_normalize_clamps_into_audio_and_drops_unusable() -> None:
    raw = [
        segment(-0.5, 0.5, "negative start"),
        segment(9.8, 10.3, "runs past the end"),
        segment(3.0, 2.0, "ends before it starts"),
        segment(math.nan, 1.0, "nan"),
        segment(1.0, math.inf, "inf"),
        segment(11.0, 12.0, "starts after the audio"),
        segment(4.0, 5.0, "   "),
    ]
    result = normalize_segments(raw, 10.0)
    assert [(s.start_s, s.end_s, s.text) for s in result.segments] == [
        (0.0, 0.5, "negative start"),
        (9.8, 10.0, "runs past the end"),
    ]
    assert result.dropped == 4  # the blank segment is skipped, not counted as malformed


def test_normalize_keeps_overlaps_and_zero_length() -> None:
    raw = [segment(0.0, 2.0, "speaker one"), segment(1.0, 1.0, "speaker two")]
    result = normalize_segments(raw, None)
    assert [(s.start_s, s.end_s) for s in result.segments] == [(0.0, 2.0), (1.0, 1.0)]


def test_normalize_words_stay_inside_their_segment() -> None:
    raw = [
        TranscriptSegment(
            1.0,
            2.0,
            "a b c d",
            (
                TranscriptWord(1.5, 1.7, " c", 0.8),
                TranscriptWord(0.8, 1.1, " a", math.nan),
                TranscriptWord(1.9, 2.4, " d", 0.7),
                TranscriptWord(1.2, 1.1, " bad", 0.5),
                TranscriptWord(1.2, 1.3, "   ", 0.5),
            ),
        )
    ]
    words = normalize_segments(raw, 5.0).segments[0].words
    assert [(w.start_s, w.end_s, w.text) for w in words] == [
        (1.0, 1.1, "a"),
        (1.5, 1.7, "c"),
        (1.9, 2.0, "d"),
    ]
    assert words[0].probability is None
    assert normalize_segments(raw, 5.0).word_count == 3


def test_normalize_empty_and_unknown_duration() -> None:
    assert normalize_segments([], 3.0).segments == ()
    result = normalize_segments([segment(100.0, 101.0, "late")], 0.0)
    assert len(result.segments) == 1  # zero duration means unknown: nothing to clamp to


def test_validate_language() -> None:
    assert validate_language(None) is None
    assert validate_language("auto") is None
    assert validate_language(" EN ") == "en"
    assert validate_language("yue") == "yue"
    for bad in ("english", "xx", "e1", "en-US"):
        with pytest.raises(ValueError, match="Unsupported language"):
            validate_language(bad)


def test_language_list_matches_installed_engine() -> None:
    tokenizer = pytest.importorskip("faster_whisper.tokenizer")
    from iftyedits.core.languages import WHISPER_LANGUAGES

    assert set(WHISPER_LANGUAGES) == set(tokenizer._LANGUAGE_CODES)


# ------------------------------------------------------------------ exports


def _stored(pos: int, start: float, end: float, text: str) -> StoredSegment:
    return StoredSegment(pos + 1, 1, pos, start, end, text, None, None, ())


def _transcript() -> Transcript:
    return Transcript(
        1, 2, 3, "a" * 64, "faster-whisper", "1.2.1", "small", "rev", "cpu", "int8", None,
        "en", 0.9, 3725.5, "h", {}, 2, 0, 0, 10.0, True, "2026-11-21T10:00:00.000000+00:00",
    )  # fmt: skip


def test_format_timestamp() -> None:
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3725.4567) == "01:02:05,457"
    assert format_timestamp(59.9996, decimal=".") == "00:01:00.000"
    with pytest.raises(ValueError):
        format_timestamp(-1)


def _srt_blocks(text: str) -> list[list[str]]:
    return [block.split("\n") for block in text.strip("\n").split("\n\n")]


def test_srt_is_well_formed() -> None:
    segments = [
        _stored(0, 0.0, 1.5, "Hello world."),
        _stored(1, 3600.25, 3600.25, "Zero length\n\nwith a blank line"),
    ]
    text = to_srt(segments)
    blocks = _srt_blocks(text)
    assert len(blocks) == 2
    for number, block in enumerate(blocks, start=1):
        assert block[0] == str(number)
        assert re.fullmatch(f"{SRT_TIME} --> {SRT_TIME}", block[1])
        assert block[2:] and all(block[2:])  # text present, no blank lines inside a cue
    assert blocks[1][1] == "01:00:00,250 --> 01:00:00,260"  # zero-length cue given 10 ms
    assert blocks[1][2:] == ["Zero length", "with a blank line"]
    assert to_srt([]) == ""


def test_vtt_is_well_formed_and_escaped() -> None:
    text = to_vtt([_stored(0, 1.0, 2.0, "a --> b & <c>")])
    lines = text.split("\n")
    assert lines[0] == "WEBVTT"
    assert lines[1] == ""
    assert re.fullmatch(f"{VTT_TIME} --> {VTT_TIME}", lines[2])
    assert lines[3] == "a → b &amp; &lt;c&gt;"
    assert to_vtt([]) == "WEBVTT\n"


def test_text_and_json_exports() -> None:
    segments = [
        StoredSegment(
            1, 1, 0, 0.0, 1.0, "Hola señor", -0.1, 0.0, (TranscriptWord(0, 0.5, "Hola", 0.9),)
        ),
        _stored(1, 1.0, 2.0, "¿Qué tal?"),
    ]
    assert to_text(segments) == "Hola señor\n¿Qué tal?\n"
    document = json.loads(export("json", _transcript(), segments))
    assert document["schema_version"] == 1
    assert document["model"] == "small" and document["audio_sha256"] == "a" * 64
    assert document["segments"][0]["words"] == [
        {"start": 0, "end": 0.5, "text": "Hola", "probability": 0.9}
    ]
    assert "señor" in export("json", _transcript(), segments)  # not \u-escaped
    with pytest.raises(ValueError, match="Unknown export format"):
        export("docx", _transcript(), segments)
