"""Settings resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits.config import Settings


def test_defaults_resolve_under_base_dir(tmp_path: Path) -> None:
    s = Settings.from_env({}, base_dir=tmp_path)
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.models_dir == (tmp_path / "models").resolve()
    assert s.log_level == "INFO"


def test_layout_keeps_originals_separate_from_outputs(tmp_path: Path) -> None:
    s = Settings.from_env({}, base_dir=tmp_path)
    for root in s.output_roots:
        assert not root.is_relative_to(s.originals_dir)
        assert not s.originals_dir.is_relative_to(root)
    assert not s.db_path.is_relative_to(s.originals_dir)


def test_absolute_env_paths_are_used(tmp_path: Path) -> None:
    s = Settings.from_env({"IFTYEDITS_DATA_DIR": str(tmp_path / "d")}, base_dir=Path("/x"))
    assert s.data_dir == (tmp_path / "d").resolve()


def test_invalid_log_level_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"IFTYEDITS_LOG_LEVEL": "loud"}, base_dir=tmp_path)
