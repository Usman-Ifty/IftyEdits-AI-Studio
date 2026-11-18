"""Domain models. Plain immutable dataclasses with no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
VFR_TOLERANCE = 0.01


class VideoStatus(StrEnum):
    """Lifecycle state of an imported video."""

    READY = "ready"
    PROBE_FAILED = "probe_failed"


class ImportOutcome(StrEnum):
    """What an import call did."""

    IMPORTED = "imported"
    DUPLICATE = "duplicate"


class JobStatus(StrEnum):
    """Background job state."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """One stream reported by ffprobe."""

    index: int
    codec_type: str
    codec_name: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    avg_frame_rate: float | None = None
    r_frame_rate: float | None = None
    rotation: int = 0
    color_transfer: str | None = None
    color_primaries: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    language: str | None = None
    bit_rate: int | None = None
    duration_s: float | None = None
    is_attached_pic: bool = False

    @property
    def is_vfr(self) -> bool:
        """True when the average frame rate differs from the nominal rate."""
        if not self.avg_frame_rate or not self.r_frame_rate:
            return False
        return abs(self.avg_frame_rate - self.r_frame_rate) / self.r_frame_rate > VFR_TOLERANCE

    @property
    def is_hdr(self) -> bool:
        """True for PQ (HDR10) or HLG transfer characteristics."""
        return self.color_transfer in HDR_TRANSFERS

    @property
    def display_size(self) -> tuple[int, int] | None:
        """Width and height after applying rotation metadata."""
        if self.width is None or self.height is None:
            return None
        if self.rotation % 180 == 90:
            return self.height, self.width
        return self.width, self.height


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Container-level probe result."""

    format_name: str | None
    duration_s: float | None
    bit_rate: int | None
    streams: tuple[StreamInfo, ...]
    raw_json: str = field(repr=False, default="{}")

    @property
    def video_streams(self) -> tuple[StreamInfo, ...]:
        """Real video streams, excluding embedded cover art."""
        return tuple(s for s in self.streams if s.codec_type == "video" and not s.is_attached_pic)

    @property
    def audio_streams(self) -> tuple[StreamInfo, ...]:
        """Audio streams."""
        return tuple(s for s in self.streams if s.codec_type == "audio")

    @property
    def primary_video(self) -> StreamInfo | None:
        """First real video stream, if any."""
        streams = self.video_streams
        return streams[0] if streams else None


@dataclass(frozen=True, slots=True)
class Video:
    """An imported original, as stored in the database."""

    id: int
    sha256: str
    vault_relpath: str
    original_filename: str
    size_bytes: int
    status: VideoStatus
    format_name: str | None
    duration_s: float | None
    bit_rate: int | None
    probe_error: str | None
    imported_at: str


@dataclass(frozen=True, slots=True)
class VideoSource:
    """A location a video was imported from. The same content may have many."""

    video_id: int
    source_path: str
    imported_at: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Outcome of importing one source file."""

    video: Video
    outcome: ImportOutcome
    source_path: Path


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of background work."""

    id: int
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    progress: float
    message: str | None
    error: str | None
    result: dict[str, Any] | None
    attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    worker_id: str | None
