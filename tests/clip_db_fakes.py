"""Database helpers for clip tests: a stored transcript and registered prepared media."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core.models import ArtifactKind, Stage, StageStatus, TranscriptSegment, Video
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.clip_fakes import write_levels_wav


def save_transcript(
    conn: sqlite3.Connection,
    video: Video,
    segments: Sequence[TranscriptSegment],
    *,
    language: str | None = "en",
    params_hash: str = "t1",
) -> int:
    duration = segments[-1].end_s + 2 if segments else 5.0
    return (
        TranscriptRepository(conn)
        .save(
            NewTranscript(
                video_id=video.id,
                audio_sha256="b" * 64,
                engine="fake",
                engine_version="1",
                model="small",
                model_revision="r",
                device="cpu",
                compute_type="int8",
                language_requested=None,
                language=language,
                language_probability=0.95,
                duration_s=duration,
                params_hash=params_hash,
                params={},
                segments=tuple(segments),
            )
        )
        .id
    )


def add_artifact(
    settings: Settings, conn: sqlite3.Connection, video: Video, kind: ArtifactKind, path: Path
) -> None:
    processing = ProcessingRepository(conn)
    stage = Stage.AUDIO if kind is ArtifactKind.AUDIO_WAV else Stage.PROXY
    record = processing.start_stage(video.id, stage, params_hash="a", params={})
    processing.register_artifact(
        video_id=video.id,
        stage_id=record.id,
        kind=kind,
        relpath=path.relative_to(settings.derived_dir.resolve()).as_posix(),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        params_hash="a",
        metadata={},
    )
    processing.finish_stage(record.id, StageStatus.SUCCEEDED)


def prepare_media(settings: Settings, conn: sqlite3.Connection, video: Video) -> tuple[Path, Path]:
    root = settings.derived_dir.resolve() / f"v{video.id}"
    audio = write_levels_wav(
        root / "audio.wav", [(20.0, 0.1), (3.0, 0.0), (7.0, 0.7), (200.0, 0.1)], rate=2000
    )
    proxy = root / "proxy.mp4"
    proxy.write_bytes(b"proxy bytes")
    add_artifact(settings, conn, video, ArtifactKind.AUDIO_WAV, audio)
    add_artifact(settings, conn, video, ArtifactKind.PROXY_MP4, proxy)
    return audio, proxy
