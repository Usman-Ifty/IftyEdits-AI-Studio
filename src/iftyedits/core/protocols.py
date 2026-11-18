"""Interfaces that services depend on. Adapters implement them."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from iftyedits.core.models import MediaInfo


class MediaProber(Protocol):
    """Reads technical metadata from a media file without modifying it."""

    def probe(self, path: Path) -> MediaInfo:
        """Return metadata for ``path`` or raise ``ProbeError``."""
        ...
