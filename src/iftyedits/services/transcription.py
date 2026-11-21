"""Transcription of imported videos from their prepared 16 kHz mono WAV.

Steps for one video:

1. Check the video can be transcribed (it exists, was probed and has an audio stream)
   and that the model is installed. Nothing is ever downloaded here.
2. Make sure the Phase 1B audio artifact exists and is intact, regenerating it from the
   original if it is missing or damaged (``MediaPrepService`` reuses a valid file).
3. Check the WAV header (16 kHz, mono, 16-bit) before handing it to the engine.
4. If a transcript with identical parameters (same audio hash, engine, model, revision
   and options) exists, reuse it unless ``force`` is set.
5. Run the engine with progress, cancellation and a time limit.
6. Validate and normalise the timings, then store transcript, segments and words in one
   transaction (after checking the job is still owned by this worker).

The original video and the WAV are only read. Earlier transcripts are kept until a new
one has been stored successfully.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from iftyedits.config import Settings
from iftyedits.core.errors import (
    InvalidSourceError,
    ModelNotInstalledError,
    NoAudioError,
    ProcessingError,
    TranscriptionError,
)
from iftyedits.core.models import (
    Artifact,
    ArtifactKind,
    ModelState,
    Stage,
    Transcript,
    TranscriptionOptions,
    TranscriptSegment,
    Video,
    VideoStatus,
)
from iftyedits.core.protocols import CancelToken, SpeechModelStore, SpeechTranscriber
from iftyedits.core.transcript import normalize_segments, validate_language
from iftyedits.services.media_prep import AUDIO_SAMPLE_RATE, OverallProgress, PrepareReport
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

TRANSCRIPT_VERSION = 1
"""Bump when normalisation or stored fields change, so old transcripts are not reused."""
AUDIO_SHARE = 0.05
"""Share of the progress bar used for checking or regenerating the audio."""
_PROGRESS_STEP_S = 0.5


class AudioPreparer(Protocol):
    """The part of ``MediaPrepService`` transcription needs."""

    def prepare(
        self,
        video_id: int,
        stages: Sequence[Stage] = ...,
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: OverallProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> PrepareReport:
        """Run media preparation stages."""
        ...

    def artifact_path(self, artifact: Artifact) -> Path:
        """Validated absolute path of an artifact."""
        ...


@dataclass(frozen=True, slots=True)
class WavInfo:
    """Header facts of a prepared WAV file."""

    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_s(self) -> float:
        """Length in seconds."""
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def clock(seconds: float) -> str:
    """``M:SS`` or ``H:MM:SS`` for progress messages."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def read_wav_info(path: Path) -> WavInfo:
    """Read and check a prepared WAV header. Raises ``TranscriptionError`` if unusable."""
    try:
        with wave.open(str(path), "rb") as handle:
            info = WavInfo(
                handle.getframerate(),
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getnframes(),
            )
    except (wave.Error, EOFError, OSError) as exc:
        raise TranscriptionError(
            f"The prepared audio file is unreadable ({exc}). Run Prepare media again "
            "(Regenerate all) and retry."
        ) from exc
    if (info.sample_rate, info.channels, info.sample_width) != (AUDIO_SAMPLE_RATE, 1, 2):
        raise TranscriptionError(
            f"The prepared audio is {info.sample_rate} Hz, {info.channels} channel(s), "
            f"{info.sample_width * 8}-bit; transcription needs 16 kHz mono 16-bit. "
            "Regenerate the media and retry."
        )
    return info


def params_hash(params: dict[str, Any]) -> str:
    """Stable 16-character hash of transcription parameters."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def options_from_settings(
    settings: Settings, *, model: str | None = None, language: str | None = None
) -> TranscriptionOptions:
    """Engine options: settings defaults, with optional model and language overrides."""
    requested = settings.transcription_language if language is None else language
    try:
        checked = validate_language(requested)
    except ValueError as exc:
        raise TranscriptionError(str(exc)) from exc
    return TranscriptionOptions(
        model=model or settings.transcription_model,
        language=checked,
        word_timestamps=settings.transcription_word_timestamps,
        vad_filter=settings.transcription_vad,
        beam_size=settings.transcription_beam_size,
        compute_type=settings.transcription_compute_type,
        cpu_threads=settings.transcription_cpu_threads,
    )


@dataclass(frozen=True, slots=True)
class TranscribeReport:
    """What one transcription run produced."""

    video_id: int
    transcript: Transcript
    reused: bool
    audio_regenerated: bool

    def as_result(self) -> dict[str, Any]:
        """JSON-serialisable job result."""
        t = self.transcript
        return {
            "video_id": self.video_id,
            "transcript_id": t.id,
            "model": t.model,
            "language": t.language,
            "language_probability": t.language_probability,
            "segments": t.segment_count,
            "words": t.word_count,
            "dropped_segments": t.dropped_segments,
            "reused": self.reused,
            "processing_s": t.processing_s,
        }

    def summary(self) -> str:
        """One-line description for the job message."""
        t = self.transcript
        if self.reused:
            return f"Reused existing {t.model} transcript ({t.segment_count} segments)"
        if t.is_empty:
            return f"No speech detected ({t.model})"
        language = t.language or "unknown language"
        return f"{t.segment_count} segments, {t.word_count} words, {language} ({t.model})"


def transcription_blocker(video: Video, has_audio: bool) -> str | None:
    """Why a video cannot be transcribed, or None if it can."""
    if video.status is VideoStatus.PROBE_FAILED:
        return "Video metadata is unavailable because probing failed at import"
    if not has_audio:
        return "This video has no audio track, so there is nothing to transcribe"
    return None


def model_missing_message(store: SpeechModelStore, name: str) -> str:
    """Actionable explanation for a model that is not installed."""
    status = store.status(name)
    size = f"{status.spec.size_bytes / 1024**2:,.0f} MB"
    state = "is incomplete" if status.state is ModelState.INCOMPLETE else "is not downloaded"
    return (
        f"Speech model '{name}' {state}. Download it ({size}) on the Transcripts page "
        f"or with `uv run iftyedits models download {name}`, then retry."
    )


class TranscriptionService:
    """Transcribes videos and stores the results."""

    def __init__(
        self,
        settings: Settings,
        videos: VideoRepository,
        processing: ProcessingRepository,
        transcripts: TranscriptRepository,
        transcriber: SpeechTranscriber,
        models: SpeechModelStore,
        media: Callable[[], AudioPreparer],
    ) -> None:
        self.settings = settings
        self.videos = videos
        self.processing = processing
        self.transcripts = transcripts
        self.transcriber = transcriber
        self.models = models
        self._media = media

    def check(self, video_id: int, options: TranscriptionOptions) -> Video:
        """Raise a clear error if this video cannot be transcribed with these options now."""
        video = self.videos.get(video_id)
        if video is None:
            raise InvalidSourceError(f"No video with id {video_id}")
        has_audio = any(s.codec_type == "audio" for s in self.videos.streams(video.id))
        blocker = transcription_blocker(video, has_audio)
        if blocker is not None and video.status is VideoStatus.PROBE_FAILED:
            raise TranscriptionError(blocker)
        if blocker is not None:
            raise NoAudioError(blocker)
        self.models.spec(options.model)  # UnknownModelError
        if self.models.status(options.model).state is not ModelState.INSTALLED:
            raise ModelNotInstalledError(model_missing_message(self.models, options.model))
        return video

    def transcribe(
        self,
        video_id: int,
        options: TranscriptionOptions,
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: OverallProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> TranscribeReport:
        """Transcribe one video. See the module docstring for the steps."""
        report_progress = progress or (lambda _f, _m: None)
        video = self.check(video_id, options)
        spec = self.models.spec(options.model)

        report_progress(0.0, "Checking prepared audio")
        audio, regenerated = self._audio(video, job_id, worker_id, report_progress, cancel)
        wav = read_wav_info(self._media().artifact_path(audio))

        params: dict[str, Any] = {
            "version": TRANSCRIPT_VERSION,
            "audio_sha256": audio.sha256,
            "engine": self.transcriber.engine,
            "model": spec.name,
            "model_revision": spec.revision,
            "language": options.language,
            "word_timestamps": options.word_timestamps,
            "vad_filter": options.vad_filter,
            "beam_size": options.beam_size,
            "compute_type": options.compute_type,
        }
        phash = params_hash(params)
        existing = self.transcripts.find(video.id, phash)
        if existing is not None and not force:
            current = self.transcripts.make_current(existing.id, job_id=job_id, worker_id=worker_id)
            report_progress(1.0, "Reused existing transcript")
            return TranscribeReport(video.id, current, reused=True, audio_regenerated=regenerated)

        started = time.monotonic()
        deadline = started + self.settings.transcription_timeout_s
        last = [0.0]

        def on_segment(segment: TranscriptSegment, duration: float | None) -> None:
            total = duration or wav.duration_s
            if not total:
                return
            if segment.end_s - last[0] < _PROGRESS_STEP_S:
                return
            last[0] = segment.end_s
            fraction = min(1.0, segment.end_s / total)
            report_progress(
                AUDIO_SHARE + (1 - AUDIO_SHARE) * fraction,
                f"Transcribing {clock(segment.end_s)} of {clock(total)}",
            )

        report_progress(AUDIO_SHARE, f"Transcribing with {spec.name}")
        if wav.frames == 0:
            language = options.language
            output_segments: tuple[TranscriptSegment, ...] = ()
            probability: float | None = None
            engine_version, device = "n/a", "none"
            duration: float | None = 0.0
        else:
            output = self.transcriber.transcribe(
                self._media().artifact_path(audio),
                self.models.status(spec.name).path,
                options,
                on_segment=on_segment,
                cancel=cancel,
                deadline=deadline,
            )
            output_segments = output.segments
            language, probability = output.language, output.language_probability
            if probability is not None:
                probability = (
                    min(1.0, max(0.0, probability)) if math.isfinite(probability) else None
                )
            engine_version, device = output.engine_version, output.device
            duration = output.duration_s if output.duration_s is not None else wav.duration_s
        normalized = normalize_segments(output_segments, duration or wav.duration_s)
        if not normalized.segments and options.language is None:
            language, probability = None, None  # a guess from silence is meaningless
        if normalized.dropped:
            logger.warning(
                "Dropped %d malformed segment(s) for video %d", normalized.dropped, video.id
            )
        report_progress(0.99, "Saving transcript")
        transcript = self.transcripts.save(
            NewTranscript(
                video_id=video.id,
                audio_sha256=audio.sha256,
                engine=self.transcriber.engine,
                engine_version=engine_version,
                model=spec.name,
                model_revision=spec.revision,
                device=device,
                compute_type=options.compute_type,
                language_requested=options.language,
                language=language,
                language_probability=probability,
                duration_s=duration,
                params_hash=phash,
                params=params,
                segments=normalized.segments,
                dropped_segments=normalized.dropped,
                processing_s=round(time.monotonic() - started, 3),
            ),
            job_id=job_id,
            worker_id=worker_id,
        )
        report_progress(1.0, "Transcript saved")
        return TranscribeReport(video.id, transcript, reused=False, audio_regenerated=regenerated)

    def _audio(
        self,
        video: Video,
        job_id: int | None,
        worker_id: str | None,
        progress: OverallProgress,
        cancel: CancelToken | None,
    ) -> tuple[Artifact, bool]:
        """The intact 16 kHz WAV artifact, regenerated from the original if needed."""

        def audio_progress(fraction: float, message: str) -> None:
            progress(AUDIO_SHARE * fraction, message)

        try:
            report = self._media().prepare(
                video.id,
                [Stage.AUDIO],
                job_id=job_id,
                worker_id=worker_id,
                progress=audio_progress,
                cancel=cancel,
            )
        except ProcessingError as exc:
            raise TranscriptionError(
                f"Could not prepare the audio for transcription: {exc}"
            ) from exc
        regenerated = not any(o.reused for o in report.outcomes)
        artifact = self.processing.artifact(video.id, ArtifactKind.AUDIO_WAV)
        if artifact is None:
            raise TranscriptionError(
                "No prepared audio is available for this video; run Prepare media first"
            )
        return artifact, regenerated
