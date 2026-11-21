"""TranscriptionService with a fake engine, fake model store and fake audio preparation."""

from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import (
    InvalidSourceError,
    JobCancelledError,
    JobOwnershipError,
    ModelNotInstalledError,
    NoAudioError,
    TranscriptionError,
    UnknownModelError,
)
from iftyedits.core.models import ArtifactKind, TranscriptionOptions, Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.services.transcription import (
    TranscriptionService,
    clock,
    options_from_settings,
    read_wav_info,
)
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from tests.conftest import MakeMedia
from tests.transcription_fakes import (
    FakeAudioPrep,
    FakeModelStore,
    FakeTranscriber,
    segment,
    write_wav,
)

OPTIONS = TranscriptionOptions(model="small")


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Talk.mp4")).video


@pytest.fixture
def engine() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def models(tmp_path: Path) -> FakeModelStore:
    return FakeModelStore(tmp_path / "models")


@pytest.fixture
def audio_prep(settings: Settings, db_conn: sqlite3.Connection) -> FakeAudioPrep:
    return FakeAudioPrep(settings, db_conn)


@pytest.fixture
def service(
    settings: Settings,
    db_conn: sqlite3.Connection,
    engine: FakeTranscriber,
    models: FakeModelStore,
    audio_prep: FakeAudioPrep,
) -> TranscriptionService:
    return TranscriptionService(
        settings,
        VideoRepository(db_conn),
        ProcessingRepository(db_conn),
        TranscriptRepository(db_conn),
        engine,
        models,
        lambda: audio_prep,
    )


def test_transcribes_and_stores(
    service: TranscriptionService,
    video: Video,
    engine: FakeTranscriber,
    db_conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    vault_file = settings.originals_dir / video.vault_relpath
    before = sha256_file(vault_file)
    progress: list[tuple[float, str]] = []
    report = service.transcribe(video.id, OPTIONS, progress=lambda f, m: progress.append((f, m)))
    t = report.transcript
    assert (t.segment_count, t.word_count, t.language, t.model) == (2, 5, "en", "small")
    assert t.is_current and not report.reused and report.audio_regenerated
    assert t.params["audio_sha256"] == t.audio_sha256
    audio = ProcessingRepository(db_conn).artifact(video.id, ArtifactKind.AUDIO_WAV)
    assert audio is not None and t.audio_sha256 == audio.sha256
    stored = TranscriptRepository(db_conn).segments(t.id)
    assert [s.text for s in stored] == ["Hello world.", "Money talks, café!"]
    assert [w.text for w in stored[0].words] == ["Hello", "world."]
    # The engine got the prepared WAV and the installed model folder.
    audio_path, model_dir, options = engine.calls[0]
    assert audio_path.suffix == ".wav" and audio_path.is_relative_to(settings.derived_dir)
    assert model_dir.name == "small" and options == OPTIONS
    fractions = [f for f, _ in progress]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert any(m.startswith("Transcribing 0:01 of 0:02") for _, m in progress)
    assert report.as_result()["segments"] == 2
    assert report.summary() == "2 segments, 5 words, en (small)"
    assert sha256_file(vault_file) == before


def test_identical_rerun_reuses_and_force_replaces(
    service: TranscriptionService,
    video: Video,
    engine: FakeTranscriber,
    db_conn: sqlite3.Connection,
) -> None:
    first = service.transcribe(video.id, OPTIONS)
    again = service.transcribe(video.id, OPTIONS)
    assert again.reused and again.transcript.id == first.transcript.id
    assert not again.audio_regenerated
    assert len(engine.calls) == 1
    assert again.summary().startswith("Reused existing small transcript")
    forced = service.transcribe(video.id, OPTIONS, force=True)
    assert not forced.reused and len(engine.calls) == 2
    assert [t.id for t in TranscriptRepository(db_conn).for_video(video.id)] == [
        forced.transcript.id
    ]


def test_other_language_adds_a_version(
    service: TranscriptionService, video: Video, db_conn: sqlite3.Connection
) -> None:
    auto = service.transcribe(video.id, OPTIONS).transcript
    spanish = service.transcribe(video.id, TranscriptionOptions("small", language="es")).transcript
    assert spanish.id != auto.id and spanish.language == "es" and spanish.language_requested == "es"
    repo = TranscriptRepository(db_conn)
    assert repo.current(video.id) == repo.get(spanish.id)
    # Asking for the first settings again makes that transcript current without re-running.
    assert service.transcribe(video.id, OPTIONS).transcript.id == auto.id
    current = repo.current(video.id)
    assert current is not None and current.id == auto.id


def test_silence_gives_an_empty_transcript_without_language(
    service: TranscriptionService, video: Video, engine: FakeTranscriber
) -> None:
    engine.segments = ()
    report = service.transcribe(video.id, OPTIONS)
    assert report.transcript.is_empty and report.transcript.language is None
    assert report.summary() == "No speech detected (small)"


def test_zero_length_audio_skips_the_engine(
    service: TranscriptionService, video: Video, engine: FakeTranscriber, audio_prep: FakeAudioPrep
) -> None:
    audio_prep.seconds = 0.0
    report = service.transcribe(video.id, OPTIONS)
    assert report.transcript.is_empty and engine.calls == []


def test_malformed_engine_segments_are_dropped(
    service: TranscriptionService, video: Video, engine: FakeTranscriber
) -> None:
    engine.segments = (
        segment(0.0, 1.0, "good"),
        segment(math.nan, 1.0, "nan"),
        segment(1.5, 1.0, "backwards"),
        segment(1.0, 99.0, "too long"),
    )
    transcript = service.transcribe(video.id, OPTIONS).transcript
    assert transcript.segment_count == 2 and transcript.dropped_segments == 2


def test_video_without_audio_is_refused(
    service: TranscriptionService, ingest: IngestService, make_media: MakeMedia
) -> None:
    from dataclasses import replace

    from tests.conftest import FakeProber

    class SilentProber(FakeProber):
        def probe(self, path: Path):  # type: ignore[no-untyped-def]
            info = super().probe(path)
            return replace(info, streams=info.video_streams)

    ingest.prober = SilentProber()
    silent = ingest.import_video(make_media("silent.mp4")).video
    with pytest.raises(NoAudioError, match="no audio track"):
        service.transcribe(silent.id, OPTIONS)


def test_missing_and_unknown_models_are_explained(
    service: TranscriptionService, video: Video, models: FakeModelStore
) -> None:
    models.installed.clear()
    with pytest.raises(ModelNotInstalledError, match="models download small"):
        service.transcribe(video.id, OPTIONS)
    with pytest.raises(UnknownModelError, match="Supported models"):
        service.transcribe(video.id, TranscriptionOptions("gigantic"))
    with pytest.raises(InvalidSourceError):
        service.transcribe(999, OPTIONS)


def test_audio_preparation_failure_is_explained(
    service: TranscriptionService, video: Video, audio_prep: FakeAudioPrep
) -> None:
    audio_prep.fail = "ffmpeg failed (exit 1): bad data"
    with pytest.raises(TranscriptionError, match=r"Could not prepare the audio.*bad data"):
        service.transcribe(video.id, OPTIONS)


@pytest.mark.parametrize(
    ("writer", "message"),
    [
        (lambda p, s: p.write_bytes(b"not a wav file at all"), "unreadable"),
        (lambda p, s: write_wav(p, s, rate=44_100), "44100 Hz"),
        (lambda p, s: write_wav(p, s, channels=2), "2 channel"),
    ],
)
def test_corrupt_or_wrong_audio_is_refused(
    service: TranscriptionService,
    video: Video,
    audio_prep: FakeAudioPrep,
    engine: FakeTranscriber,
    writer: object,
    message: str,
) -> None:
    audio_prep.wav_writer = writer  # type: ignore[assignment]
    with pytest.raises(TranscriptionError, match=message):
        service.transcribe(video.id, OPTIONS)
    assert engine.calls == []


def test_engine_failure_keeps_previous_transcript(
    service: TranscriptionService,
    video: Video,
    engine: FakeTranscriber,
    db_conn: sqlite3.Connection,
) -> None:
    first = service.transcribe(video.id, OPTIONS).transcript
    engine.behaviour = "error"
    with pytest.raises(TranscriptionError, match="boom"):
        service.transcribe(video.id, OPTIONS, force=True)
    assert TranscriptRepository(db_conn).current(video.id) == first


def test_cancellation_stores_nothing(
    service: TranscriptionService,
    video: Video,
    engine: FakeTranscriber,
    db_conn: sqlite3.Connection,
) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        service.transcribe(video.id, OPTIONS, cancel=cancel)
    assert TranscriptRepository(db_conn).for_video(video.id) == []


def test_lost_ownership_stores_nothing(
    service: TranscriptionService, video: Video, db_conn: sqlite3.Connection
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    assert queue.claim("w1") is not None
    with pytest.raises(JobOwnershipError):
        service.transcribe(video.id, OPTIONS, job_id=job_id, worker_id="w2")
    assert TranscriptRepository(db_conn).for_video(video.id) == []
    report = service.transcribe(video.id, OPTIONS, job_id=job_id, worker_id="w1")
    assert report.transcript.job_id == job_id


def test_options_from_settings(settings: Settings) -> None:
    options = options_from_settings(settings)
    assert (options.model, options.language, options.compute_type) == ("small", None, "int8")
    assert options_from_settings(settings, model="tiny", language="EN").language == "en"
    with pytest.raises(TranscriptionError, match="Unsupported language"):
        options_from_settings(settings, language="klingon")


def test_read_wav_info_and_clock(tmp_path: Path) -> None:
    info = read_wav_info(write_wav(tmp_path / "a.wav", 1.5))
    assert (info.sample_rate, info.channels, info.frames, info.duration_s) == (16000, 1, 24000, 1.5)
    assert clock(5.9) == "0:05" and clock(3725) == "1:02:05"
