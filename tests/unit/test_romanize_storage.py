# ruff: noqa: F811
"""Stored Hindi transcripts are read, captioned and exported in Roman letters."""

from __future__ import annotations

import sqlite3

from iftyedits.core.captions import build_cues
from iftyedits.core.models import TranscriptSegment, TranscriptWord, Video
from iftyedits.core.shorts import CaptionStyle
from iftyedits.storage.transcripts import TranscriptRepository
from tests.clip_db_fakes import save_transcript
from tests.unit.test_ui_clips_page import video  # noqa: F401


def test_captions_and_search_results_are_roman_and_the_stored_text_is_kept(
    db_conn: sqlite3.Connection, video: Video
) -> None:
    words = (TranscriptWord(0.0, 0.8, " क्या", 0.9), TranscriptWord(0.8, 1.6, " बात", 0.9))
    transcript_id = save_transcript(
        db_conn, video, [TranscriptSegment(0.0, 1.6, " क्या बात", words, -0.2, 0.01)], language="hi"
    )
    repository = TranscriptRepository(db_conn)
    segments = repository.segments(transcript_id)
    assert segments[0].text.strip() == "kya baat"
    cues = build_cues(segments, 0.0, 1.6, CaptionStyle())
    assert [c.text for c in cues] == ["kya baat"] and cues[0].can_highlight
    hit = repository.search("बात")[0]
    assert hit.text.strip() == "kya baat"
    assert hit.snippet.strip() == "kya \x02baat\x03"  # the whole word is marked
    stored = db_conn.execute("SELECT text FROM transcript_segments").fetchone()[0]
    assert "क्या" in stored


def test_stray_characters_are_removed_only_from_hindi_transcripts(
    db_conn: sqlite3.Connection, video: Video
) -> None:
    said = [TranscriptSegment(0.0, 1.0, " ठीक 的", (), -0.2, 0.01)]
    repository = TranscriptRepository(db_conn)
    hindi = save_transcript(db_conn, video, said, language="hi")
    chinese = save_transcript(db_conn, video, said, language="zh", params_hash="t2")
    assert repository.segments(hindi)[0].text.strip() == "theek"
    assert "的" in repository.segments(chinese)[0].text
