"""Where the newest video stands on the seven-step Shorts workflow (read-only)."""

from __future__ import annotations

import sqlite3

from iftyedits.core.models import ALL_STAGES, StageStatus
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.shorts import KIND_EXPORT, ShortRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository

STEP_COUNT = 7
"""Matches the number of steps in ``iftyedits.ui.theme.JOURNEY``."""


def latest_progress(conn: sqlite3.Connection) -> tuple[tuple[bool, ...], str]:
    """``(flags, video name)`` for the newest video, one flag per step of ``ui.theme.JOURNEY``.

    Steps: link, import, analyze (media prepared and transcribed), clips generated, candidates
    reviewed (any favourite or reject), Short edited (a project exists), exported (a finished
    render exists). With no video every flag is false and the name is empty.
    """
    latest = VideoRepository(conn).list_recent(limit=1)
    if not latest:
        return (False,) * STEP_COUNT, ""
    video = latest[0]
    stages = ProcessingRepository(conn).stage_summary().get(video.id, {})
    prepared = all(
        stages.get(s) in (StageStatus.SUCCEEDED, StageStatus.SKIPPED) for s in ALL_STAGES
    )
    heard = video.id in TranscriptRepository(conn).current_by_video()
    found = video.id in ClipRepository(conn).current_by_video()
    reviewed = bool(ClipRepository(conn).decision_counts(video.id))
    shorts = ShortRepository(conn)
    projects = shorts.list_projects(video.id)
    exported = any(shorts.list_renders(p.id, KIND_EXPORT) for p in projects)
    flags = (True, True, prepared and heard, found, reviewed, bool(projects), exported)
    return flags, video.original_filename
