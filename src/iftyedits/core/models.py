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
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True once the job can no longer change state."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class CancelOutcome(StrEnum):
    """Result of a cancellation request."""

    CANCELLED = "cancelled"
    """The job was queued and is now cancelled."""
    REQUESTED = "requested"
    """The job is running; its worker will stop it."""
    NOT_ACTIVE = "not_active"
    """The job had already finished."""
    NOT_FOUND = "not_found"


class Stage(StrEnum):
    """Media preparation stages, in execution order."""

    AUDIO = "audio"
    PROXY = "proxy"
    THUMBNAIL = "thumbnail"


ALL_STAGES: tuple[Stage, ...] = (Stage.AUDIO, Stage.PROXY, Stage.THUMBNAIL)


class StageStatus(StrEnum):
    """State of one stage for one video."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ArtifactKind(StrEnum):
    """Kinds of generated files."""

    AUDIO_WAV = "audio_wav"
    PROXY_MP4 = "proxy_mp4"
    THUMBNAIL_JPG = "thumbnail_jpg"


STAGE_ARTIFACT: dict[Stage, ArtifactKind] = {
    Stage.AUDIO: ArtifactKind.AUDIO_WAV,
    Stage.PROXY: ArtifactKind.PROXY_MP4,
    Stage.THUMBNAIL: ArtifactKind.THUMBNAIL_JPG,
}


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
    time_base: str | None = None
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
    cancel_requested_at: str | None = None

    @property
    def video_id(self) -> int | None:
        """Video a processing job works on, if any."""
        value = self.payload.get("video_id")
        return value if isinstance(value, int) else None


@dataclass(frozen=True, slots=True)
class StageRecord:
    """Latest state of one processing stage for one video."""

    id: int
    video_id: int
    stage: Stage
    status: StageStatus
    job_id: int | None
    params_hash: str
    params: dict[str, Any]
    progress: float
    message: str | None
    error: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class Artifact:
    """A generated file registered in the database."""

    id: int
    video_id: int
    stage_id: int
    job_id: int | None
    kind: ArtifactKind
    relpath: str
    size_bytes: int
    sha256: str
    params_hash: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class EncoderInfo:
    """H.264 encoder chosen for proxies and whether hardware encoding was verified."""

    name: str
    hardware: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TranscoderCapabilities:
    """What the installed FFmpeg can do, as detected (not assumed)."""

    binary: str
    version: str
    encoders: frozenset[str]
    filters: frozenset[str]
    h264: EncoderInfo | None
    missing: tuple[str, ...]
    """Required encoders or filters that are not available."""


# ---------------------------------------------------------------------- transcription


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    """One recognised word with its timing (seconds from the start of the audio)."""

    start_s: float
    end_s: float
    text: str
    probability: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One recognised phrase. ``words`` is empty when the engine gives no word timings."""

    start_s: float
    end_s: float
    text: str
    words: tuple[TranscriptWord, ...] = ()
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionOptions:
    """What to transcribe with. ``language=None`` means automatic detection."""

    model: str
    language: str | None = None
    word_timestamps: bool = True
    vad_filter: bool = True
    beam_size: int = 5
    compute_type: str = "int8"
    cpu_threads: int = 0


@dataclass(frozen=True, slots=True)
class EngineTranscription:
    """Raw output of a speech engine, before validation and normalisation."""

    engine: str
    engine_version: str
    device: str
    compute_type: str
    language: str | None
    language_probability: float | None
    duration_s: float | None
    segments: tuple[TranscriptSegment, ...]


@dataclass(frozen=True, slots=True)
class Transcript:
    """A stored transcript of one video's prepared audio.

    ``is_current`` marks the transcript later pipeline stages should use (one per video).
    """

    id: int
    video_id: int
    job_id: int | None
    audio_sha256: str
    engine: str
    engine_version: str
    model: str
    model_revision: str | None
    device: str
    compute_type: str
    language_requested: str | None
    language: str | None
    language_probability: float | None
    duration_s: float | None
    params_hash: str
    params: dict[str, Any]
    segment_count: int
    word_count: int
    dropped_segments: int
    processing_s: float | None
    is_current: bool
    created_at: str

    @property
    def is_empty(self) -> bool:
        """True when no speech was recognised."""
        return self.segment_count == 0


@dataclass(frozen=True, slots=True)
class StoredSegment:
    """A transcript segment as stored, with its position and database id."""

    id: int
    transcript_id: int
    position: int
    start_s: float
    end_s: float
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None
    words: tuple[TranscriptWord, ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptSearchHit:
    """A segment matching a search, from a video's current transcript."""

    video_id: int
    original_filename: str
    transcript_id: int
    segment_id: int
    position: int
    start_s: float
    end_s: float
    text: str
    snippet: str
    """Segment text with matches wrapped in ``SEARCH_MATCH_START``/``SEARCH_MATCH_END``."""


SEARCH_MATCH_START = "\x02"
SEARCH_MATCH_END = "\x03"


class ModelState(StrEnum):
    """Whether a speech model is available locally."""

    INSTALLED = "installed"
    MISSING = "missing"
    INCOMPLETE = "incomplete"
    """Some files are missing or have the wrong size; download it again."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One file of a downloadable model, with its expected size and SHA-256 (if published)."""

    name: str
    size_bytes: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechModelSpec:
    """A supported speech model and the exact files to download for it."""

    name: str
    repo_id: str
    revision: str
    files: tuple[ModelFile, ...]
    summary: str
    ram_gb: float
    """Approximate peak memory while transcribing on CPU with int8: measured for tiny and
    base, estimated for the others (docs/DECISIONS.md D16)."""

    @property
    def size_bytes(self) -> int:
        """Total download size."""
        return sum(f.size_bytes for f in self.files)


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """Local state of one speech model."""

    spec: SpeechModelSpec
    state: ModelState
    path: Path
    detail: str = ""
