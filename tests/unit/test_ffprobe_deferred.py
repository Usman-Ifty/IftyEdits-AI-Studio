"""The editor's prober must not need ffprobe until something is probed."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iftyedits.adapters.ffprobe import DeferredFFprobe
from iftyedits.config import Settings
from iftyedits.core.errors import ExternalToolError
from iftyedits.services.shorts import build_short_service

MISSING = "/nonexistent/ffprobe"


def test_a_missing_ffprobe_is_reported_only_when_probing(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    prober = DeferredFFprobe(MISSING)
    with pytest.raises(ExternalToolError, match="ffprobe not found"):
        prober.probe(media)


def test_the_editor_service_builds_without_ffprobe(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    without = settings.model_copy(update={"ffprobe_path": MISSING})
    assert build_short_service(without, db_conn).prober is not None
