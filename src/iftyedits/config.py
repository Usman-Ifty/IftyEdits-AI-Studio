"""Application settings from environment variables and an optional ``.env`` file.

Precedence: process environment > ``.env`` > defaults. Relative paths resolve
against the project home: ``IFTYEDITS_HOME`` if set, otherwise the working directory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

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
    min_free_bytes: int = 2 * 1024**3
    video_extensions: Annotated[tuple[str, ...], NoDecode] = DEFAULT_VIDEO_EXTENSIONS

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

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
