"""Application settings from environment variables and an optional ``.env`` file.

Precedence: process environment > ``.env`` > defaults. Relative paths resolve
against the project home: ``IFTYEDITS_HOME`` if set, otherwise the working directory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
EncoderPreference = Literal["auto", "h264_videotoolbox", "libx264"]
ComputeType = Literal["int8", "int8_float32", "float32"]

DEFAULT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".mts",
    ".m2ts",
    ".mxf",
)


def project_home() -> Path:
    """Directory that relative paths and ``.env`` resolve against."""
    home = os.environ.get("IFTYEDITS_HOME")
    return Path(home).expanduser().resolve() if home else Path.cwd().resolve()


class Settings(BaseSettings):
    """Resolved application paths and options. Immutable."""

    model_config = SettingsConfigDict(
        env_prefix="IFTYEDITS_",
        env_file=None,
        extra="ignore",
        frozen=True,
    )

    data_dir: Path = Path("data")
    models_dir: Path = Path("models")
    log_level: LogLevel = "INFO"
    ffprobe_path: str = "ffprobe"
    probe_timeout_s: float = 60.0
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout_s: float = Field(default=6 * 3600.0, gt=0)
    proxy_encoder: EncoderPreference = "auto"
    export_encoder: EncoderPreference = "auto"
    min_free_bytes: int = 2 * 1024**3
    transcription_model: str = "small"
    transcription_language: str = "auto"
    transcription_compute_type: ComputeType = "int8"
    transcription_cpu_threads: int = Field(default=0, ge=0, le=64)
    transcription_beam_size: int = Field(default=5, ge=1, le=10)
    transcription_vad: bool = True
    transcription_word_timestamps: bool = True
    transcription_timeout_s: float = Field(default=6 * 3600.0, gt=0)
    clip_min_s: float = Field(default=15.0, gt=0, le=600)
    clip_target_s: float = Field(default=40.0, gt=0, le=600)
    clip_max_s: float = Field(default=60.0, gt=0, le=600)
    clip_max_candidates: int = Field(default=20, ge=1, le=200)
    url_max_duration_s: float = Field(default=4 * 3600.0, gt=0)
    url_download_timeout_s: float = Field(default=3600.0, gt=0)
    url_max_filesize_mb: int = Field(default=4096, ge=50)
    clip_max_overlap: float = Field(default=0.5, ge=0, le=1)
    clip_scene_threshold: float = Field(default=0.3, ge=0.05, le=0.95)
    clip_use_audio: bool = True
    clip_use_scenes: bool = True
    video_extensions: Annotated[tuple[str, ...], NoDecode] = DEFAULT_VIDEO_EXTENSIONS

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("transcription_language", mode="before")
    @classmethod
    def _check_language(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower() or "auto"
            if value != "auto" and not (
                value.isascii() and value.isalpha() and 2 <= len(value) <= 3
            ):
                raise ValueError("transcription_language must be 'auto' or a code such as 'en'")
        return value

    @model_validator(mode="after")
    def _check_clip_durations(self) -> Settings:
        if not self.clip_min_s <= self.clip_target_s <= self.clip_max_s:
            raise ValueError(
                "clip durations must satisfy clip_min_s <= clip_target_s <= clip_max_s"
            )
        return self

    @field_validator("video_extensions", mode="before")
    @classmethod
    def _split_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in value.split(",") if part.strip()]
        if isinstance(value, list | tuple):
            items = [str(v).strip().lower() for v in value]
            return tuple(v if v.startswith(".") else f".{v}" for v in items)
        return value

    @property
    def originals_dir(self) -> Path:
        """Read-only vault of imported original media."""
        return self.data_dir / "originals"

    @property
    def staging_dir(self) -> Path:
        """In-progress import copies. Same filesystem as the vault, outside it."""
        return self.data_dir / "staging"

    @property
    def derived_dir(self) -> Path:
        """Generated intermediate files (audio, proxies, clips)."""
        return self.data_dir / "derived"

    @property
    def exports_dir(self) -> Path:
        """Finished, publishable output."""
        return self.data_dir / "exports"

    @property
    def db_path(self) -> Path:
        """SQLite database file."""
        return self.data_dir / "db" / "iftyedits.sqlite"

    @property
    def output_roots(self) -> tuple[Path, ...]:
        """Directories where generated files may be written."""
        return (self.derived_dir, self.exports_dir)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, base_dir: Path | None = None
    ) -> Settings:
        """Build settings and resolve relative paths against ``base_dir``.

        With ``environ=None`` the process environment and ``<base_dir>/.env`` are read.
        With an explicit mapping only that mapping is used (no ``.env``), which keeps
        tests hermetic.
        """
        base = (base_dir or project_home()).resolve()
        if environ is None:
            loaded = cls(_env_file=base / ".env")
        else:
            prefix = cls.model_config.get("env_prefix", "")
            values = {
                name: environ[f"{prefix}{name}".upper()]
                for name in cls.model_fields
                if f"{prefix}{name}".upper() in environ
            }
            loaded = cls.model_validate(values)
        return loaded.model_copy(
            update={
                "data_dir": _resolve(loaded.data_dir, base),
                "models_dir": _resolve(loaded.models_dir, base),
            }
        )


def _resolve(value: Path, base_dir: Path) -> Path:
    path = value.expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()
