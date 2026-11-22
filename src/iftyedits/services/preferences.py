"""Small remembered interface choices (for example "hide the welcome guide").

Stored as one JSON file under the data folder. A missing or damaged file means defaults;
it never raises, because losing a preference must not stop the app.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
FILE_NAME = "ui_preferences.json"
KNOWN = {"welcome_dismissed": bool}


class Preferences:
    """Read and write a few boolean preferences."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / FILE_NAME

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: bool = False) -> bool:
        """The stored value, or ``default`` when unset or not a boolean."""
        value = self._load().get(key, default)
        return value if isinstance(value, bool) else default

    def set(self, key: str, value: bool) -> bool:
        """Store a preference atomically. Returns False if it could not be saved."""
        if key not in KNOWN:
            raise KeyError(f"Unknown preference {key!r}")
        data = {**self._load(), key: bool(value)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".prefs-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            Path(name).replace(self.path)
        except OSError as exc:
            logger.warning("Could not save preferences: %s", exc)
            return False
        return True
