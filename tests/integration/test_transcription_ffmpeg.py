"""Transcription with real FFmpeg audio preparation (Phase 1C).

The speech engine is a fake unless the opt-in ``model`` tests are enabled with
``IFTYEDITS_RUN_MODEL_TESTS=1`` and an installed model (see docs/TESTING.md).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from iftyedits.adapters.faster_whisper import FasterWhisperTranscriber
from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.adapters.model_store import ModelStore
from iftyedits.config import Settings
from iftyedits.core.errors import NoAudioError
from iftyedits.core.models import ArtifactKind, ModelState, TranscriptionOptions, Video
from iftyedits.core.protocols import SpeechModelStore, SpeechTranscriber
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.services.media_prep import MediaPrepService
from iftyedits.services.subtitles import to_srt, to_vtt
from iftyedits.services.transcription import TranscriptionService
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from tests.integration.conftest import FFPROBE, run_ffmpeg
from tests.transcription_fakes import FakeModelStore, FakeTranscriber, segment

pytestmark = pytest.mark.ffmpeg

Build = Callable[[SpeechTranscriber, SpeechModelStore], TranscriptionService]


@pytest.fixture
def real_ingest(settings: Settings, db_conn: sqlite3.Connection) -> IngestService:
    return IngestService(
        settings, VideoRepository(db_conn), FFprobe(timeout_s=60), build_vault(settings)
    )


@pytest.fixture
def build(settings: Settings, db_conn: sqlite3.Connection, ffmpeg: str) -> Build:
    media = MediaPrepService(
        settings,
        VideoRepository(db_conn),
        ProcessingRepository(db_conn),
        FFmpeg(ffmpeg, 120.0),
        FFprobe(timeout_s=60),
    )

    def make(engine: SpeechTranscriber, models: SpeechModelStore) -> TranscriptionService:
        return TranscriptionService(
            settings,
            VideoRepository(db_conn),
            ProcessingRepository(db_conn),
            TranscriptRepository(db_conn),
            engine,
            models,
            lambda: media,
        )

    return make


@pytest.fixture
def clip(media_dir: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "user" / "clip.mp4"
    copy.parent.mkdir()
    shutil.copy2(media_dir / "clip.mp4", copy)
    return copy


def _ffprobe_subtitles(path: Path) -> tuple[str, int]:
    """(codec name, packet count) as FFmpeg's own demuxer reads the file."""
    assert FFPROBE is not None
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "s:0", "-count_packets",
         "-show_entries", "stream=codec_name,nb_read_packets", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    codec, packets = result.stdout.strip().split(",")
    return codec, int(packets)


def test_transcription_uses_prepared_audio_and_preserves_original(
    build: Build, real_ingest: IngestService, clip: Path, settings: Settings, tmp_path: Path,
    db_conn: sqlite3.Connection,
) -> None:  # fmt: skip
    source_hash = sha256_file(clip)
    video = real_ingest.import_video(clip).video
    engine = FakeTranscriber(
        (segment(0.0, 0.9, " Tone one."), segment(1.0, 1.9, " Tone --> two & <three>")),
        duration=None,
    )
    service = build(engine, FakeModelStore(tmp_path / "models"))
    report = service.transcribe(video.id, TranscriptionOptions("small"))
    assert report.audio_regenerated and report.transcript.segment_count == 2
    audio = ProcessingRepository(db_conn).artifact(video.id, ArtifactKind.AUDIO_WAV)
    assert audio is not None and report.transcript.audio_sha256 == audio.sha256
    assert report.transcript.duration_s == pytest.approx(2.0, abs=0.1)  # from the WAV header
    wav_path = engine.calls[0][0]
    assert wav_path == (settings.derived_dir / audio.relpath).resolve()

    # Subtitle exports are accepted by FFmpeg's own SRT and WebVTT demuxers.
    segments = TranscriptRepository(db_conn).segments(report.transcript.id)
    srt, vtt = tmp_path / "out.srt", tmp_path / "out.vtt"
    srt.write_text(to_srt(segments), encoding="utf-8")
    vtt.write_text(to_vtt(segments), encoding="utf-8")
    assert _ffprobe_subtitles(srt) == ("subrip", 2)
    assert _ffprobe_subtitles(vtt) == ("webvtt", 2)

    # A damaged WAV is regenerated from the original before transcribing again.
    wav = settings.derived_dir / audio.relpath
    wav.chmod(0o644)
    wav.write_bytes(b"RIFF" + b"\x00" * 64)
    again = service.transcribe(video.id, TranscriptionOptions("small"), force=True)
    assert again.audio_regenerated and len(engine.calls) == 2
    vault_copy = settings.originals_dir / video.vault_relpath
    assert sha256_file(clip) == source_hash == sha256_file(vault_copy) == video.sha256


def test_video_without_audio_is_refused(
    build: Build, real_ingest: IngestService, media_dir: Path, tmp_path: Path
) -> None:
    video = real_ingest.import_video(media_dir / "silent.mov").video
    service = build(FakeTranscriber(), FakeModelStore(tmp_path / "models"))
    with pytest.raises(NoAudioError):
        service.transcribe(video.id, TranscriptionOptions("small"))


# ---------------------------------------------------------------------- opt-in real model

REAL_MODEL = os.environ.get("IFTYEDITS_TEST_MODEL", "tiny")
REAL_MODELS_DIR = Path(
    os.environ.get("IFTYEDITS_TEST_MODELS_DIR", Path(__file__).resolve().parents[2] / "models")
)
SAY = shutil.which("say")
SENTENCE = "Hello and welcome to the show. Today we talk about money and video games."


@pytest.fixture
def real_model_store() -> ModelStore:
    if os.environ.get("IFTYEDITS_RUN_MODEL_TESTS") != "1":
        pytest.skip("real-model test; set IFTYEDITS_RUN_MODEL_TESTS=1 to run it")
    pytest.importorskip("faster_whisper")
    store = ModelStore(REAL_MODELS_DIR)
    if store.status(REAL_MODEL).state is not ModelState.INSTALLED:
        pytest.skip(f"model {REAL_MODEL} is not installed in {REAL_MODELS_DIR}")
    return store


@pytest.fixture
def speech_video(ffmpeg: str, tmp_path: Path) -> Path:
    if SAY is None:
        pytest.skip("macOS `say` is needed to synthesise speech")
    aiff = tmp_path / "speech.aiff"
    subprocess.run([SAY, "-o", str(aiff), SENTENCE], check=True, timeout=60)
    out = tmp_path / "user" / "speech.mp4"
    out.parent.mkdir()
    run_ffmpeg(
        ffmpeg, "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25", "-i", str(aiff),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(out),
    )  # fmt: skip
    return out


@pytest.mark.model
@pytest.mark.slow
def test_real_model_transcribes_speech(
    build: Build,
    real_ingest: IngestService,
    real_model_store: ModelStore,
    speech_video: Path,
    db_conn: sqlite3.Connection,
) -> None:
    video: Video = real_ingest.import_video(speech_video).video
    service = build(FasterWhisperTranscriber(), real_model_store)
    report = service.transcribe(video.id, TranscriptionOptions(REAL_MODEL))
    transcript = report.transcript
    assert transcript.language == "en" and transcript.segment_count >= 1
    text = " ".join(s.text for s in TranscriptRepository(db_conn).segments(transcript.id)).lower()
    for word in ("welcome", "money", "video"):
        assert word in text
    segments = TranscriptRepository(db_conn).segments(transcript.id)
    assert transcript.duration_s is not None
    for seg in segments:
        assert 0 <= seg.start_s <= seg.end_s <= transcript.duration_s
        assert all(seg.start_s <= w.start_s <= w.end_s <= seg.end_s for w in seg.words)
    assert transcript.word_count >= 10
    assert TranscriptRepository(db_conn).search("money")
