"""Test doubles for transcription: WAV writer, fake engine, fake model store, fake audio prep.

No model is downloaded and no speech engine runs when these are used.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import wave
from collections.abc import Sequence
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core.errors import (
    JobCancelledError,
    ProcessingError,
    TranscriptionError,
    UnknownModelError,
)
from iftyedits.core.models import (
    Artifact,
    ArtifactKind,
    EngineTranscription,
    ModelFile,
    ModelState,
    ModelStatus,
    SpeechModelSpec,
    Stage,
    StageStatus,
    TranscriptionOptions,
    TranscriptSegment,
    TranscriptWord,
)
from iftyedits.core.protocols import ByteProgress, CancelToken, SegmentCallback
from iftyedits.services.media_prep import OverallProgress, PrepareReport, StageOutcome
from iftyedits.storage.originals import assert_safe_output_path, sha256_file
from iftyedits.storage.processing import ProcessingRepository


def write_wav(
    path: Path, seconds: float = 2.0, *, rate: int = 16_000, channels: int = 1, width: int = 2
) -> Path:
    """Write a silent PCM WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * frames * channels * width)
    return path


def segment(
    start: float, end: float, text: str, *words: tuple[float, float, str]
) -> TranscriptSegment:
    """Short constructor for engine segments."""
    return TranscriptSegment(
        start, end, text, tuple(TranscriptWord(s, e, w, 0.9) for s, e, w in words), -0.2, 0.01
    )


DEFAULT_SEGMENTS = (
    segment(0.0, 0.8, " Hello world.", (0.0, 0.4, " Hello"), (0.4, 0.8, " world.")),
    segment(0.9, 1.9, " Money talks, café!", (0.9, 1.3, " Money"), (1.3, 1.6, " talks,"),
            (1.6, 1.9, " café!")),
)  # fmt: skip


class FakeTranscriber:
    """Returns scripted segments; can fail, wait for cancellation or run past a deadline."""

    engine = "fake-engine"

    def __init__(
        self,
        segments: Sequence[TranscriptSegment] = DEFAULT_SEGMENTS,
        *,
        behaviour: str = "ok",
        language: str | None = "en",
        probability: float | None = 0.97,
        duration: float | None = 2.0,
    ) -> None:
        self.segments = tuple(segments)
        self.behaviour = behaviour
        self.language = language
        self.probability = probability
        self.duration = duration
        self.calls: list[tuple[Path, Path, TranscriptionOptions]] = []
        self.started = threading.Event()

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
        self.calls.append((audio, model_dir, options))
        self.started.set()
        if self.behaviour == "error":
            raise TranscriptionError("Speech recognition failed: RuntimeError: boom")
        if self.behaviour == "wait_for_cancel":
            limit = time.monotonic() + 20
            while cancel is None or not cancel.is_set():
                if time.monotonic() > limit:
                    raise AssertionError("cancel never arrived")
                time.sleep(0.01)
            raise JobCancelledError("Transcription cancelled")
        for seg in self.segments:
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Transcription cancelled")
            if on_segment is not None:
                on_segment(seg, self.duration)
        language = options.language or self.language
        return EngineTranscription(
            engine=self.engine,
            engine_version="0.0-test",
            device="cpu",
            compute_type=options.compute_type,
            language=language,
            language_probability=1.0 if options.language else self.probability,
            duration_s=self.duration,
            segments=self.segments,
        )


TEST_SPEC = SpeechModelSpec(
    name="small",
    repo_id="test/faster-whisper-small",
    revision="0" * 40,
    files=(ModelFile("model.bin", 10, None), ModelFile("config.json", 2, None)),
    summary="test model",
    ram_gb=1.0,
)
OTHER_SPEC = SpeechModelSpec(
    name="tiny",
    repo_id="test/faster-whisper-tiny",
    revision="1" * 40,
    files=(ModelFile("model.bin", 5, None),),
    summary="test tiny",
    ram_gb=0.5,
)


class FakeModelStore:
    """In-memory model store; ``installed`` names are reported as installed."""

    def __init__(self, root: Path, installed: Sequence[str] = ("small",)) -> None:
        self.root = root
        self.installed = set(installed)
        self.installs: list[str] = []
        self.fail_with: Exception | None = None

    def catalog(self) -> tuple[SpeechModelSpec, ...]:
        return (OTHER_SPEC, TEST_SPEC)

    def spec(self, name: str) -> SpeechModelSpec:
        for spec in self.catalog():
            if spec.name == name:
                return spec
        raise UnknownModelError(f"Unknown speech model {name!r}. Supported models: tiny, small")

    def status(self, name: str) -> ModelStatus:
        spec = self.spec(name)
        state = ModelState.INSTALLED if name in self.installed else ModelState.MISSING
        return ModelStatus(spec, state, self.root / name)

    def statuses(self) -> list[ModelStatus]:
        return [self.status(s.name) for s in self.catalog()]

    def install(
        self,
        name: str,
        *,
        progress: ByteProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> ModelStatus:
        spec = self.spec(name)
        self.installs.append(name)
        if self.fail_with is not None:
            raise self.fail_with
        if progress is not None:
            progress(spec.size_bytes // 2, spec.size_bytes)
            progress(spec.size_bytes, spec.size_bytes)
        self.installed.add(name)
        return self.status(name)

    def clean_staging(self) -> int:
        return 0


class FakeAudioPrep:
    """Registers a 16 kHz WAV artifact like MediaPrepService's audio stage would."""

    def __init__(
        self, settings: Settings, conn: sqlite3.Connection, *, seconds: float = 2.0
    ) -> None:
        self.settings = settings
        self.processing = ProcessingRepository(conn)
        self.seconds = seconds
        self.calls = 0
        self.fail: str | None = None
        self.wav_writer = write_wav

    def prepare(
        self,
        video_id: int,
        stages: Sequence[Stage] = (Stage.AUDIO,),
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: OverallProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> PrepareReport:
        self.calls += 1
        if self.fail is not None:
            raise ProcessingError(f"1 stage(s) failed. audio: {self.fail}")
        report = PrepareReport(video_id)
        existing = self.processing.artifact(video_id, ArtifactKind.AUDIO_WAV)
        if existing is not None and (self.settings.derived_dir / existing.relpath).is_file():
            report.outcomes.append(
                StageOutcome(Stage.AUDIO, StageStatus.SUCCEEDED, "reused", existing, reused=True)
            )
            return report
        path = assert_safe_output_path(
            self.settings.derived_dir / f"v{video_id}" / "audio.wav",
            originals_root=self.settings.originals_dir,
            allowed_roots=(self.settings.derived_dir,),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.wav_writer(path, self.seconds)
        record = self.processing.start_stage(
            video_id, Stage.AUDIO, params_hash="a", params={}, job_id=job_id, worker_id=worker_id
        )
        artifact, _ = self.processing.register_artifact(
            video_id=video_id,
            stage_id=record.id,
            kind=ArtifactKind.AUDIO_WAV,
            relpath=path.relative_to(self.settings.derived_dir.resolve()).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
            params_hash="a",
            metadata={"sample_rate": 16000, "channels": 1, "duration_s": self.seconds},
            job_id=job_id,
            worker_id=worker_id,
        )
        self.processing.finish_stage(
            record.id, StageStatus.SUCCEEDED, job_id=job_id, worker_id=worker_id
        )
        if progress is not None:
            progress(1.0, "audio: done")
        report.outcomes.append(StageOutcome(Stage.AUDIO, StageStatus.SUCCEEDED, "made", artifact))
        return report

    def artifact_path(self, artifact: Artifact) -> Path:
        return self.settings.derived_dir / artifact.relpath
