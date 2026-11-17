"""Shared pytest fixtures. Tests never touch the real data/ directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.storage.originals import OriginalsVault


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a temporary directory."""
    s = Settings(data_dir=tmp_path / "data", models_dir=tmp_path / "models")
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
