"""Application settings read from environment variables.

Uses only the standard library for now. ``.env`` file loading will be added
with the first phase that needs it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved application paths and options."""

    data_dir: Path
    models_dir: Path
    log_level: str = "INFO"

    @property
    def originals_dir(self) -> Path:
        """Read-only vault of imported original media."""
        return self.data_dir / "originals"

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
        cls, environ: Mapping[str, str] | None = None, base_dir: Path = PROJECT_ROOT
    ) -> Settings:
        """Build settings from environment variables, resolving relative paths."""
        env = os.environ if environ is None else environ
        level = env.get("IFTYEDITS_LOG_LEVEL", "INFO").upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"Invalid IFTYEDITS_LOG_LEVEL: {level!r}")
        return cls(
            data_dir=_resolve(env.get("IFTYEDITS_DATA_DIR", "data"), base_dir),
            models_dir=_resolve(env.get("IFTYEDITS_MODELS_DIR", "models"), base_dir),
            log_level=level,
        )


def _resolve(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()
