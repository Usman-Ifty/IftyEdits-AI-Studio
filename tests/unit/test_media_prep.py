"""Media preparation logic that does not need FFmpeg: sizing, paths, guards, cleanup."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.errors import (
    ExternalToolError,
    InvalidSourceError,
    JobCancelledError,
    OriginalProtectionError,
    ProcessingError,
)
from iftyedits.core.models import (
    EncoderInfo,
    Stage,
    StageStatus,
    StreamInfo,
    TranscoderCapabilities,
    Video,
)
from iftyedits.core.protocols import CancelToken, SecondsCallback
from iftyedits.services.ingest import IngestService
from iftyedits.services.media_prep import (
    MediaPrepService,
    clean_partials,
    durations_match,
    params_hash,
    proxy_fps,
    proxy_size,
    thumbnail_seek,
)
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.videos import VideoRepository
from tests.conftest import FakeProber, MakeMedia


class FakeTranscoder:
    """Writes junk to the output path (last argument), optionally failing."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def capabilities(self) -> TranscoderCapabilities:
        return TranscoderCapabilities(
            "ffmpeg", "test", frozenset(), frozenset(), EncoderInfo("libx264", False, "test"), ()
        )

    def run(
        self,
        args: Sequence[str],
        *,
        progress: SecondsCallback | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        redact: Sequence[Path] = (),
    ) -> None:
        self.calls.append(list(args))
        Path(args[-1]).write_bytes(b"not really media")
        if progress is not None:
            progress(1.0)
        if self.fail:
            raise ExternalToolError("ffmpeg failed (exit 1): boom")


def _service(
    settings: Settings, conn: sqlite3.Connection, transcoder: FakeTranscoder
) -> MediaPrepService:
    return MediaPrepService(
        settings, VideoRepository(conn), ProcessingRepository(conn), transcoder, FakeProber()
    )


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("talk.mp4")).video


# ------------------------------------------------------------------ pure functions


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1920, 1080), (1280, 720)),
        ((1080, 1920), (720, 1280)),
        ((3840, 2160), (1280, 720)),
        ((1280, 720), (1280, 720)),
        ((320, 240), (320, 240)),  # never upscaled
        ((321, 241), (320, 240)),  # odd sizes made even
        ((2560, 1080), (1706, 720)),  # ultra-wide keeps its aspect ratio
        ((1440, 1080), (960, 720)),
    ],
)
def test_proxy_size(size: tuple[int, int], expected: tuple[int, int]) -> None:
    assert proxy_size(*size) == expected


@pytest.mark.parametrize(
    ("avg", "nominal", "fps"),
    [(60.0, 60.0, 30.0), (23.976, 23.976, 23.976), (None, None, 30.0), (16.36, 30.0, 16.36)],
)
def test_proxy_fps(avg: float | None, nominal: float | None, fps: float) -> None:
    stream = StreamInfo(0, "video", avg_frame_rate=avg, r_frame_rate=nominal)
    assert proxy_fps(stream) == fps


def test_thumbnail_seek_skips_intro_but_handles_short_videos() -> None:
    assert thumbnail_seek(None) == 0.0
    assert thumbnail_seek(1.0) == 0.0
    assert thumbnail_seek(100.0) == 10.0


def test_durations_match() -> None:
    assert durations_match(None, 5.0)
    assert durations_match(10.0, 10.9)
    assert not durations_match(10.0, 11.5)
    assert durations_match(3600.0, 3660.0)  # 2% of long videos
    assert not durations_match(10.0, None)


def test_params_hash_is_stable_and_order_independent() -> None:
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})
    assert params_hash({"a": 1}) != params_hash({"a": 2})
    assert len(params_hash({})) == 16


def test_output_paths_are_deterministic_and_derived(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    service = _service(settings, db_conn, FakeTranscoder())
    path = service.output_path(video, Stage.PROXY, "0123456789abcdef")
    assert path == (
        settings.derived_dir / video.sha256[:2] / video.sha256 / "proxy-720p-0123456789abcdef.mp4"
    )
    assert path == service.output_path(video, Stage.PROXY, "0123456789abcdef")


# ------------------------------------------------------------------ guards


def test_failed_validation_leaves_no_partial_and_no_artifact(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    service = _service(settings, db_conn, FakeTranscoder())
    original = settings.originals_dir / video.vault_relpath
    before = original.read_bytes()
    with pytest.raises(ProcessingError, match="3 stage"):
        service.prepare(video.id)
    processing = ProcessingRepository(db_conn)
    assert processing.artifacts(video.id) == []
    stages = processing.stages(video.id)
    assert [s.status for s in stages] == [StageStatus.FAILED] * 3
    assert all(s.error and "cannot be read" in s.error for s in stages)
    assert not [p for p in settings.derived_dir.rglob("*") if p.is_file()]
    assert original.read_bytes() == before


def test_ffmpeg_failure_is_recorded_per_stage_and_others_continue(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    transcoder = FakeTranscoder(fail=True)
    service = _service(settings, db_conn, transcoder)
    with pytest.raises(ProcessingError) as info:
        service.prepare(video.id, [Stage.AUDIO, Stage.PROXY])
    assert "audio: ffmpeg failed" in str(info.value)
    assert len(transcoder.calls) == 2  # proxy still ran after audio failed
    assert not list(settings.derived_dir.rglob("*.partial"))


def test_cancel_before_a_stage_starts_runs_nothing(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    transcoder = FakeTranscoder()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        _service(settings, db_conn, transcoder).prepare(video.id, cancel=cancel)
    assert transcoder.calls == []


def test_probe_failed_video_cannot_be_processed(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    db_conn.execute("UPDATE videos SET status = 'probe_failed', probe_error = 'bad'")
    db_conn.execute("DELETE FROM media_streams")
    transcoder = FakeTranscoder()
    with pytest.raises(ProcessingError, match="probing failed"):
        _service(settings, db_conn, transcoder).prepare(video.id)
    assert transcoder.calls == []


def test_missing_original_or_unknown_video(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    service = _service(settings, db_conn, FakeTranscoder())
    with pytest.raises(InvalidSourceError, match="No video"):
        service.prepare(999)
    db_conn.execute("UPDATE videos SET vault_relpath = 'zz/missing.mp4'")
    with pytest.raises(InvalidSourceError, match="missing from the vault"):
        service.prepare(video.id)
    db_conn.execute("UPDATE videos SET vault_relpath = '../../outside.mp4'")
    with pytest.raises(OriginalProtectionError):
        service.prepare(video.id)


def test_derived_symlink_into_vault_is_refused(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    (settings.derived_dir / video.sha256[:2]).symlink_to(settings.originals_dir)
    vault_files = sorted(p for p in settings.originals_dir.rglob("*") if p.is_file())
    transcoder = FakeTranscoder()
    with pytest.raises(OriginalProtectionError):
        _service(settings, db_conn, transcoder).prepare(video.id, [Stage.PROXY])
    assert transcoder.calls == []
    assert sorted(p for p in settings.originals_dir.rglob("*") if p.is_file()) == vault_files


def test_clean_partials_only_removes_old_partial_files(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    (derived / "ab").mkdir(parents=True)
    old = derived / "ab" / ".proxy.mp4.1.partial"
    new = derived / "ab" / ".audio.wav.2.partial"
    real = derived / "ab" / "proxy.mp4"
    outside = tmp_path / "keep.partial"
    for path in (old, new, real, outside):
        path.write_bytes(b"x")
    link = derived / "ab" / ".link.partial"
    link.symlink_to(outside)
    for path in (old, real, outside):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    assert clean_partials(derived) == 1
    assert not old.exists()
    assert new.exists() and real.exists() and outside.exists() and link.is_symlink()
    assert clean_partials(tmp_path / "missing") == 0
