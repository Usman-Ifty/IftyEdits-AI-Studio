"""Media preparation with real FFmpeg on generated media (Phase 1B)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.config import Settings
from iftyedits.core.errors import ExternalToolError, JobCancelledError, ProcessingError
from iftyedits.core.models import ArtifactKind, JobStatus, Stage, StageStatus, Video
from iftyedits.core.protocols import CancelToken, SecondsCallback
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.services.media_prep import MediaPrepService
from iftyedits.storage.db import open_database
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.videos import VideoRepository
from tests.integration.conftest import run_ffmpeg

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="module")
def prep_media(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generated sources covering the shapes Phase 1B must handle."""
    root = tmp_path_factory.mktemp("prep_media")
    h264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]

    def src(size: str, rate: int = 30, seconds: int = 2) -> list[str]:
        return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]

    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    stereo = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "2"]
    run_ffmpeg(
        ffmpeg, *src("320x240"), *tone, *h264, "-c:a", "aac", "-shortest", str(root / "talk.mp4")
    )
    run_ffmpeg(
        ffmpeg,
        *src("1920x1080", rate=60),
        *stereo,
        *h264,
        "-c:a",
        "aac",
        "-shortest",
        str(root / "hd.mp4"),
    )
    run_ffmpeg(ffmpeg, *src("1080x1920"), *h264, "-an", str(root / "portrait.mov"))
    run_ffmpeg(ffmpeg, *src("321x241", rate=25), "-c:v", "ffv1", str(root / "odd.mkv"))
    run_ffmpeg(
        ffmpeg, "-display_rotation", "90", "-i", str(root / "talk.mp4"), "-c", "copy",
        str(root / "rotated.mp4"),
    )  # fmt: skip
    run_ffmpeg(
        ffmpeg, *src("320x240"), "-vf", "select='lt(mod(n\\,10)\\,5)'", "-fps_mode", "vfr",
        *h264, str(root / "vfr.mp4"),
    )  # fmt: skip
    return root


@pytest.fixture
def real_ingest(settings: Settings, db_conn: sqlite3.Connection) -> IngestService:
    return IngestService(
        settings, VideoRepository(db_conn), FFprobe(timeout_s=60), build_vault(settings)
    )


@pytest.fixture
def service(settings: Settings, db_conn: sqlite3.Connection) -> MediaPrepService:
    return MediaPrepService(
        settings,
        VideoRepository(db_conn),
        ProcessingRepository(db_conn),
        FFmpeg(timeout_s=300),
        FFprobe(timeout_s=60),
    )


@pytest.fixture
def imported(real_ingest: IngestService, prep_media: Path) -> dict[str, Video]:
    """Import every generated source into this test's vault."""
    return {
        path.name: real_ingest.import_video(path).video for path in sorted(prep_media.iterdir())
    }


def _artifacts(conn: sqlite3.Connection, video: Video) -> dict[ArtifactKind, Path]:
    """Registered artifact files, each checked against its recorded size and hash."""
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    derived = db_file.parents[1] / "derived"
    found: dict[ArtifactKind, Path] = {}
    for artifact in ProcessingRepository(conn).artifacts(video.id):
        path = derived / artifact.relpath
        assert path.is_file()
        assert sha256_file(path) == artifact.sha256
        assert path.stat().st_size == artifact.size_bytes
        found[artifact.kind] = path
    return found


def test_full_preparation_outputs_and_originals(
    service: MediaPrepService,
    imported: dict[str, Video],
    db_conn: sqlite3.Connection,
    settings: Settings,
    prep_media: Path,
) -> None:
    sources_before = {p.name: sha256_file(p) for p in prep_media.iterdir()}
    video = imported["hd.mp4"]
    report = service.prepare(video.id)
    assert [o.status for o in report.outcomes] == [StageStatus.SUCCEEDED] * 3

    files = _artifacts(db_conn, video)
    assert set(files) == set(ArtifactKind)
    probe = FFprobe()
    audio = probe.probe(files[ArtifactKind.AUDIO_WAV])
    (a,) = audio.audio_streams
    assert (a.codec_name, a.sample_rate, a.channels) == ("pcm_s16le", 16000, 1)
    assert audio.duration_s is not None and abs(audio.duration_s - 2.0) < 0.2

    proxy = probe.probe(files[ArtifactKind.PROXY_MP4])
    v = proxy.primary_video
    assert v is not None and (v.codec_name, v.width, v.height) == ("h264", 1280, 720)
    assert v.avg_frame_rate is not None and abs(v.avg_frame_rate - 30) < 0.5
    assert proxy.audio_streams and proxy.audio_streams[0].codec_name == "aac"

    thumb = probe.probe(files[ArtifactKind.THUMBNAIL_JPG])
    t = thumb.primary_video
    assert t is not None and (t.codec_name, t.width, t.height) == ("mjpeg", 640, 360)

    for path in files.values():
        assert path.is_relative_to(settings.derived_dir)
    assert not list(settings.derived_dir.rglob("*.partial"))
    vault_copy = settings.originals_dir / video.vault_relpath
    assert sha256_file(vault_copy) == video.sha256
    assert {p.name: sha256_file(p) for p in prep_media.iterdir()} == sources_before

    proxy_artifact = ProcessingRepository(db_conn).artifact(video.id, ArtifactKind.PROXY_MP4)
    assert proxy_artifact is not None
    meta = proxy_artifact.metadata
    assert meta["encoder"] in ("h264_videotoolbox", "libx264")
    assert meta["hardware_encoded"] is (meta["encoder"] == "h264_videotoolbox")


@pytest.mark.parametrize(
    ("name", "size", "has_audio"),
    [
        ("talk.mp4", (320, 240), True),
        ("rotated.mp4", (240, 320), True),  # display rotation applied
        ("portrait.mov", (720, 1280), False),
        ("odd.mkv", (320, 240), False),  # ffv1, odd dimensions
        ("vfr.mp4", (320, 240), False),
    ],
)
def test_proxy_shapes(
    service: MediaPrepService,
    imported: dict[str, Video],
    db_conn: sqlite3.Connection,
    name: str,
    size: tuple[int, int],
    has_audio: bool,
) -> None:
    video = imported[name]
    report = service.prepare(video.id)
    statuses = {o.stage: o.status for o in report.outcomes}
    assert statuses[Stage.PROXY] is StageStatus.SUCCEEDED
    assert statuses[Stage.THUMBNAIL] is StageStatus.SUCCEEDED
    expected_audio = StageStatus.SUCCEEDED if has_audio else StageStatus.SKIPPED
    assert statuses[Stage.AUDIO] is expected_audio

    files = _artifacts(db_conn, video)
    assert (ArtifactKind.AUDIO_WAV in files) is has_audio
    proxy = FFprobe().probe(files[ArtifactKind.PROXY_MP4])
    v = proxy.primary_video
    assert v is not None and (v.width, v.height) == size
    assert v.rotation == 0
    assert bool(proxy.audio_streams) is has_audio
    if name == "vfr.mp4":
        assert v.avg_frame_rate is not None and v.r_frame_rate is not None
        assert not v.is_vfr


def test_rerun_reuses_valid_outputs_and_repairs_damaged_ones(
    service: MediaPrepService, imported: dict[str, Video], db_conn: sqlite3.Connection
) -> None:
    video = imported["talk.mp4"]
    service.prepare(video.id)
    files = _artifacts(db_conn, video)
    mtimes = {k: p.stat().st_mtime_ns for k, p in files.items()}

    again = service.prepare(video.id)
    assert all(o.reused for o in again.outcomes)
    assert {k: p.stat().st_mtime_ns for k, p in files.items()} == mtimes

    files[ArtifactKind.AUDIO_WAV].write_bytes(b"damaged")
    repaired = service.prepare(video.id)
    reused = {o.stage: o.reused for o in repaired.outcomes}
    assert reused == {Stage.AUDIO: False, Stage.PROXY: True, Stage.THUMBNAIL: True}
    _artifacts(db_conn, video)  # hashes match again

    forced = service.prepare(video.id, [Stage.THUMBNAIL], force=True)
    assert not forced.outcomes[0].reused


def test_cancel_during_proxy_keeps_completed_audio(
    service: MediaPrepService, imported: dict[str, Video], db_conn: sqlite3.Connection,
    settings: Settings,
) -> None:  # fmt: skip
    video = imported["hd.mp4"]
    cancel = threading.Event()

    def progress(_: float, message: str) -> None:
        if message.startswith("proxy"):
            cancel.set()

    with pytest.raises(JobCancelledError):
        service.prepare(video.id, progress=progress, cancel=cancel)
    repo = ProcessingRepository(db_conn)
    statuses = {s.stage: s.status for s in repo.stages(video.id)}
    assert statuses == {Stage.AUDIO: StageStatus.SUCCEEDED, Stage.PROXY: StageStatus.CANCELLED}
    files = _artifacts(db_conn, video)
    assert set(files) == {ArtifactKind.AUDIO_WAV}
    assert not list(settings.derived_dir.rglob("*.partial"))
    assert not list(settings.derived_dir.rglob("proxy-*"))


class BrokenProxyEncoder(FFmpeg):
    """Real FFmpeg, but proxy encodes use an encoder that does not exist."""

    def run(
        self,
        args: Sequence[str],
        *,
        progress: SecondsCallback | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        redact: Sequence[Path] = (),
    ) -> None:
        patched = ["no_such_encoder" if a in ("libx264", "h264_videotoolbox") else a for a in args]
        super().run(patched, progress=progress, cancel=cancel, timeout_s=timeout_s, redact=redact)


def test_process_failure_is_recorded_and_other_stages_finish(
    settings: Settings,
    db_conn: sqlite3.Connection,
    imported: dict[str, Video],
) -> None:
    service = MediaPrepService(
        settings,
        VideoRepository(db_conn),
        ProcessingRepository(db_conn),
        BrokenProxyEncoder(),
        FFprobe(),
    )
    video = imported["talk.mp4"]
    with pytest.raises(ProcessingError, match="proxy: ffmpeg failed"):
        service.prepare(video.id)
    repo = ProcessingRepository(db_conn)
    statuses = {s.stage: s.status for s in repo.stages(video.id)}
    assert statuses == {
        Stage.AUDIO: StageStatus.SUCCEEDED,
        Stage.PROXY: StageStatus.FAILED,
        Stage.THUMBNAIL: StageStatus.SUCCEEDED,
    }
    proxy = repo.stage(video.id, Stage.PROXY)
    assert proxy is not None and proxy.error is not None
    assert str(settings.data_dir) not in proxy.error
    thumb = repo.artifact(video.id, ArtifactKind.THUMBNAIL_JPG)
    assert thumb is not None and thumb.metadata["input"] == "original"
    assert not list(settings.derived_dir.rglob("*.partial"))


def test_real_ffmpeg_cancel_and_timeout(ffmpeg: str) -> None:
    endless = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "null", "-"]
    runner = FFmpeg(ffmpeg, poll_interval_s=0.05)
    cancel = threading.Event()
    seen: list[float] = []

    def on_progress(seconds: float) -> None:
        seen.append(seconds)
        if seconds > 0.5:
            cancel.set()

    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        runner.run(endless, progress=on_progress, cancel=cancel, timeout_s=60)
    assert seen and time.monotonic() - started < 30

    with pytest.raises(ExternalToolError, match="timed out"):
        runner.run(endless, timeout_s=0.5)


def test_capabilities_detect_a_working_h264_encoder(ffmpeg: str) -> None:
    caps = FFmpeg(ffmpeg).capabilities()
    assert caps.h264 is not None
    assert caps.missing == ()
    software = FFmpeg(ffmpeg, encoder="libx264").capabilities()
    assert software.h264 is not None and software.h264.name == "libx264"
    assert not software.h264.hardware


def test_worker_imports_and_prepares_end_to_end(settings: Settings, prep_media: Path) -> None:
    source = prep_media / "talk.mp4"
    before = sha256_file(source)
    conn = open_database(settings.db_path)
    try:
        queue = JobQueue(conn)
        (import_id,) = queue.enqueue_imports([source], process_after=True)
        worker = Worker(settings, worker_id="it-worker", heartbeat_interval_s=0.05)
        try:
            assert worker.run(once=True) == 2
        finally:
            worker.close()
        imported = queue.get(import_id)
        assert imported is not None and imported.result is not None
        process = queue.get(imported.result["process_job_id"])
        assert process is not None
        assert process.status is JobStatus.SUCCEEDED, process.error
        assert process.result is not None
        assert process.result["stages"] == {
            "audio": "succeeded",
            "proxy": "succeeded",
            "thumbnail": "succeeded",
        }
        artifacts = ProcessingRepository(conn).artifacts(imported.result["video_id"])
        assert {a.job_id for a in artifacts} == {process.id}
    finally:
        conn.close()
    assert sha256_file(source) == before
    assert os.access(source, os.W_OK)  # the user's file is untouched, not made read-only
