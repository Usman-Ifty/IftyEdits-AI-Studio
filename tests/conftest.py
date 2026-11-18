"""Shared pytest fixtures. Tests never touch the real data/ directory."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import ProbeError
from iftyedits.core.models import MediaInfo, StreamInfo
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.storage.db import open_database
from iftyedits.storage.originals import OriginalsVault
from iftyedits.storage.videos import VideoRepository

FAKE_VIDEO = b"FAKEVIDEO"
FAKE_AUDIO = b"FAKEAUDIO"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a temporary directory."""
    s = Settings(data_dir=tmp_path / "data", models_dir=tmp_path / "models", min_free_bytes=0)
    for directory in (s.originals_dir, *s.output_roots):
        directory.mkdir(parents=True)
    return s


@pytest.fixture
def vault(settings: Settings) -> OriginalsVault:
    """An empty originals vault."""
    return OriginalsVault(settings.originals_dir)


@pytest.fixture
def source_video(tmp_path: Path) -> Path:
    """A small fake video file outside the data directory."""
    src_dir = tmp_path / "user_media"
    src_dir.mkdir()
    path = src_dir / "Holiday Clip.MP4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 64)
    return path


class FakeProber:
    """Recognises files by a magic prefix instead of running ffprobe."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def probe(self, path: Path) -> MediaInfo:
        self.calls.append(path)
        with path.open("rb") as handle:
            head = handle.read(len(FAKE_VIDEO))
        if head == FAKE_VIDEO:
            streams = (
                StreamInfo(0, "video", "h264", width=640, height=360, avg_frame_rate=30.0),
                StreamInfo(1, "audio", "aac", sample_rate=48000, channels=2),
            )
            return MediaInfo("mov,mp4", 2.0, 1000, streams, "{}")
        if head == FAKE_AUDIO:
            return MediaInfo("mp3", 2.0, 1000, (StreamInfo(0, "audio", "mp3"),), "{}")
        raise ProbeError(f"Invalid data found when processing input: {path.name}")


MakeMedia = Callable[..., Path]


@pytest.fixture
def make_media(tmp_path: Path) -> MakeMedia:
    """Create a fake media file under ``tmp_path/user_media``."""
    root = tmp_path / "user_media"

    def make(name: str, *, kind: bytes = FAKE_VIDEO, body: bytes | None = None) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(kind + (body if body is not None else os.urandom(4096)))
        return path

    return make


@pytest.fixture
def db_conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    """Migrated database in the temporary data directory."""
    conn = open_database(settings.db_path)
    yield conn
    conn.close()


@pytest.fixture
def prober() -> FakeProber:
    """Fake media prober."""
    return FakeProber()


@pytest.fixture
def ingest(settings: Settings, db_conn: sqlite3.Connection, prober: FakeProber) -> IngestService:
    """Ingest service wired to temporary storage and the fake prober."""
    return IngestService(settings, VideoRepository(db_conn), prober, build_vault(settings))
