"""Interfaces that services depend on. Adapters implement them."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from iftyedits.core.clip_models import SceneCut
from iftyedits.core.framing import FaceBox
from iftyedits.core.models import (
    EngineTranscription,
    MediaInfo,
    ModelStatus,
    SpeechModelSpec,
    TranscoderCapabilities,
    TranscriptionOptions,
    TranscriptSegment,
)
from iftyedits.core.sources import SourceMetadata, VideoUrl


class FaceDetector(Protocol):
    """Finds where faces are in a still picture. It never identifies anyone."""

    def detect(self, image: Path) -> list[FaceBox]:
        """Faces in ``image``, or raise ``FaceDetectionError``."""
        ...


class MediaProber(Protocol):
    """Reads technical metadata from a media file without modifying it."""

    def probe(self, path: Path) -> MediaInfo:
        """Return metadata for ``path`` or raise ``ProbeError``."""
        ...


class CancelToken(Protocol):
    """Cooperative cancellation flag (``threading.Event`` satisfies it)."""

    def is_set(self) -> bool:
        """True once cancellation was requested."""
        ...


SecondsCallback = Callable[[float], None]
"""Called with the media time (seconds) processed so far."""


class MediaTranscoder(Protocol):
    """Runs FFmpeg-style transcodes as external processes."""

    def capabilities(self) -> TranscoderCapabilities:
        """Detected encoders, filters and the verified H.264 encoder."""
        ...

    def run(
        self,
        args: Sequence[str],
        *,
        progress: SecondsCallback | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        redact: Sequence[Path] = (),
    ) -> None:
        """Run one command to completion.

        Raises ``JobCancelledError`` after the process has exited when ``cancel`` is
        set, and ``ExternalToolError`` on failure or timeout. ``redact`` paths are
        replaced by their file names in error messages.
        """
        ...


SegmentCallback = Callable[[TranscriptSegment, float | None], None]
"""Called with each recognised segment and the total audio duration (if known)."""

ByteProgress = Callable[[int, int], None]
"""Called with ``(bytes_done, bytes_total)``."""


class SpeechTranscriber(Protocol):
    """Turns a 16 kHz mono WAV file into timestamped text. Never modifies the audio."""

    engine: str

    def transcribe(
        self,
        audio: Path,
        model_dir: Path,
        options: TranscriptionOptions,
        *,
        on_segment: SegmentCallback | None = None,
        cancel: CancelToken | None = None,
        deadline: float | None = None,
    ) -> EngineTranscription:
        """Transcribe ``audio`` with the model stored in ``model_dir``.

        ``deadline`` is a ``time.monotonic()`` value. Raises ``JobCancelledError`` when
        ``cancel`` is set, ``TranscriptionTimeoutError`` after the deadline and another
        ``TranscriptionError`` if the engine or model fails.
        """
        ...


class SpeechModelStore(Protocol):
    """Downloads and locates speech models in the local models directory."""

    def catalog(self) -> tuple[SpeechModelSpec, ...]:
        """Supported models, smallest first."""
        ...

    def spec(self, name: str) -> SpeechModelSpec:
        """One model's specification. Raises ``UnknownModelError``."""
        ...

    def status(self, name: str) -> ModelStatus:
        """Whether the model is installed. Never downloads anything."""
        ...

    def install(
        self,
        name: str,
        *,
        progress: ByteProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> ModelStatus:
        """Download and verify a model. Raises ``ModelDownloadError`` on failure."""
        ...

    def clean_staging(self) -> int:
        """Remove abandoned partial downloads. Returns how many were removed."""
        ...


class SceneDetector(Protocol):
    """Finds scene changes in a (proxy) video without modifying it."""

    def detect(
        self,
        video: Path,
        *,
        threshold: float,
        duration_s: float | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> list[SceneCut]:
        """Scene cuts with their scores. Raises ``ExternalToolError`` if decoding fails."""
        ...


class SourceDownloader(Protocol):
    """Fetches metadata for, and downloads, one video from a validated URL."""

    def fetch_metadata(self, url: VideoUrl) -> SourceMetadata:
        """Ask the site about the video. Downloads nothing."""
        ...

    def download(
        self,
        url: VideoUrl,
        directory: Path,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """Download into ``directory`` (which must be empty and writable); return the file."""
        ...


class TextModel(Protocol):
    """A language model that answers one prompt with text. Adapters implement it."""

    @property
    def name(self) -> str:
        """Model identifier recorded with every generated result."""
        ...

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        """Return the model's reply or raise a :class:`~iftyedits.core.errors.LlmError`."""
        ...
