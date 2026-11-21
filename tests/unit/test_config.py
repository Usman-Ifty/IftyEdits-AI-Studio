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


def test_processing_settings(tmp_path: Path) -> None:
    defaults = Settings.from_env({}, base_dir=tmp_path)
    assert (defaults.ffmpeg_path, defaults.proxy_encoder) == ("ffmpeg", "auto")
    assert defaults.ffmpeg_timeout_s == 6 * 3600
    custom = Settings.from_env(
        {"IFTYEDITS_PROXY_ENCODER": "libx264", "IFTYEDITS_FFMPEG_TIMEOUT_S": "90"},
        base_dir=tmp_path,
    )
    assert (custom.proxy_encoder, custom.ffmpeg_timeout_s) == ("libx264", 90.0)
    for name, value in (("PROXY_ENCODER", "nvenc"), ("FFMPEG_TIMEOUT_S", "0")):
        with pytest.raises(ValueError):
            Settings.from_env({f"IFTYEDITS_{name}": value}, base_dir=tmp_path)


def test_clip_settings_defaults_and_overrides(tmp_path: Path) -> None:
    s = Settings.from_env({}, base_dir=tmp_path)
    assert (s.clip_min_s, s.clip_target_s, s.clip_max_s) == (15.0, 40.0, 60.0)
    assert s.clip_max_candidates == 20 and s.clip_use_audio and s.clip_use_scenes
    s = Settings.from_env(
        {"IFTYEDITS_CLIP_MIN_S": "10", "IFTYEDITS_CLIP_USE_SCENES": "false"}, base_dir=tmp_path
    )
    assert s.clip_min_s == 10.0 and not s.clip_use_scenes


@pytest.mark.parametrize(
    "env",
    [
        {"IFTYEDITS_CLIP_MIN_S": "50"},  # above the default target
        {"IFTYEDITS_CLIP_MAX_S": "20"},  # below the default target
        {"IFTYEDITS_CLIP_MIN_S": "0"},
        {"IFTYEDITS_CLIP_MAX_OVERLAP": "1.5"},
        {"IFTYEDITS_CLIP_MAX_CANDIDATES": "0"},
        {"IFTYEDITS_CLIP_SCENE_THRESHOLD": "0.99"},
    ],
)
def test_invalid_clip_settings_are_refused(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r"clip|greater|less|validation"):
        Settings.from_env(env, base_dir=tmp_path)
