"""Transcript exports: SubRip (SRT), WebVTT, plain text and JSON. Pure functions.

Both subtitle formats get one cue per segment. Cue text never contains blank lines
(they end a cue) and WebVTT text never contains ``-->`` or raw ``&``/``<``/``>``.
A zero-length segment is given a 10 ms cue so players do not discard it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from iftyedits.core.models import StoredSegment, Transcript, TranscriptSegment

EXPORT_FORMATS = ("srt", "vtt", "txt", "json")
MIN_CUE_S = 0.01
EXPORT_SCHEMA_VERSION = 1

Segment = TranscriptSegment | StoredSegment


def format_timestamp(seconds: float, *, decimal: str = ",") -> str:
    """``HH:MM:SS,mmm`` (SRT) or ``HH:MM:SS.mmm`` (WebVTT) for a non-negative time."""
    if seconds < 0:
        raise ValueError("Timestamps cannot be negative")
    total_ms = round(seconds * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal}{millis:03d}"


def _cue_times(segment: Segment) -> tuple[float, float]:
    end = max(segment.end_s, segment.start_s + MIN_CUE_S)
    return segment.start_s, end


def _single_paragraph(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def to_srt(segments: Sequence[Segment]) -> str:
    """SubRip text. An empty transcript gives an empty file."""
    blocks: list[str] = []
    for segment in segments:
        text = _single_paragraph(segment.text)
        if not text:
            continue
        start, end = _cue_times(segment)
        blocks.append(
            f"{len(blocks) + 1}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n"
        )
    return "\n".join(blocks)


def _vtt_escape(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("--&gt;", "→")


def to_vtt(segments: Sequence[Segment]) -> str:
    """WebVTT text. An empty transcript gives just the ``WEBVTT`` header."""
    blocks = ["WEBVTT\n"]
    for segment in segments:
        text = _vtt_escape(_single_paragraph(segment.text))
        if not text:
            continue
        start, end = _cue_times(segment)
        stamp_start = format_timestamp(start, decimal=".")
        stamp_end = format_timestamp(end, decimal=".")
        blocks.append(f"{stamp_start} --> {stamp_end}\n{text}\n")
    return "\n".join(blocks)


def to_text(segments: Sequence[Segment]) -> str:
    """One line per segment."""
    lines = [_single_paragraph(s.text).replace("\n", " ") for s in segments]
    return "\n".join(line for line in lines if line) + ("\n" if lines else "")


def to_json(transcript: Transcript, segments: Sequence[StoredSegment]) -> str:
    """Machine-readable export with provenance, segments and word timings."""
    document: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "transcript_id": transcript.id,
        "video_id": transcript.video_id,
        "audio_sha256": transcript.audio_sha256,
        "engine": transcript.engine,
        "engine_version": transcript.engine_version,
        "model": transcript.model,
        "model_revision": transcript.model_revision,
        "language": transcript.language,
        "language_probability": transcript.language_probability,
        "duration_s": transcript.duration_s,
        "created_at": transcript.created_at,
        "segments": [
            {
                "start": s.start_s,
                "end": s.end_s,
                "text": s.text,
                "words": [
                    {
                        "start": w.start_s,
                        "end": w.end_s,
                        "text": w.text,
                        "probability": w.probability,
                    }
                    for w in s.words
                ],
            }
            for s in segments
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def export(fmt: str, transcript: Transcript, segments: Sequence[StoredSegment]) -> str:
    """Render ``segments`` in one of :data:`EXPORT_FORMATS`."""
    if fmt == "srt":
        return to_srt(segments)
    if fmt == "vtt":
        return to_vtt(segments)
    if fmt == "txt":
        return to_text(segments)
    if fmt == "json":
        return to_json(transcript, segments)
    raise ValueError(f"Unknown export format {fmt!r}; choose one of {', '.join(EXPORT_FORMATS)}")
