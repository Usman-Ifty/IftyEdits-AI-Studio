"""End-to-end import with real FFmpeg-generated media and real ffprobe."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from iftyedits import cli
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.config import Settings
from iftyedits.core.errors import UnsupportedMediaError
from iftyedits.core.models import ImportOutcome, JobStatus, VideoStatus
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.videos import VideoRepository

pytestmark = pytest.mark.ffmpeg


@pytest.fixture
def real_ingest(settings: Settings, db_conn: sqlite3.Connection) -> IngestService:
    return IngestService(settings, VideoRepository(db_conn), FFprobe(), build_vault(settings))


@pytest.fixture
def user_copy(media_dir: Path, tmp_path: Path) -> Path:
    """A per-test copy of the generated media folder (the user's 'originals')."""
    target = tmp_path / "my_videos"
    shutil.copytree(media_dir, target)
    return target


def test_ffprobe_reads_generated_clip(media_dir: Path) -> None:
    info = FFprobe().probe(media_dir / "clip.mp4")
    video = info.primary_video
    assert video is not None
    assert (video.codec_name, video.width, video.height) == ("h264", 320, 240)
    assert video.avg_frame_rate == pytest.approx(30.0)
    assert video.time_base is not None
    assert info.audio_streams[0].codec_name == "aac"
    assert info.duration_s == pytest.approx(2.0, abs=0.2)


def test_full_import_preserves_source(
    real_ingest: IngestService, user_copy: Path, settings: Settings
) -> None:
    source = user_copy / "clip.mp4"
    before = (sha256_file(source), source.stat().st_mtime_ns, source.stat().st_mode)

    result = real_ingest.import_video(source)

    assert result.outcome is ImportOutcome.IMPORTED
    video = result.video
    assert video.status is VideoStatus.READY
    assert video.duration_s == pytest.approx(2.0, abs=0.2)
    streams = real_ingest.repo.streams(video.id)
    assert [(s.codec_type, s.codec_name) for s in streams] == [("video", "h264"), ("audio", "aac")]
    stored = settings.originals_dir / video.vault_relpath
    assert sha256_file(stored) == video.sha256 == before[0]
    assert stored.stat().st_mode & 0o222 == 0
    assert stored.stat().st_ino != source.stat().st_ino
    assert (sha256_file(source), source.stat().st_mtime_ns, source.stat().st_mode) == before


def test_renamed_copy_with_other_extension_is_duplicate(
    real_ingest: IngestService, user_copy: Path, settings: Settings
) -> None:
    original = user_copy / "clip.mp4"
    renamed = user_copy / "Final Cut v2.MOV"
    shutil.copyfile(original, renamed)

    first = real_ingest.import_video(original)
    second = real_ingest.import_video(renamed)

    assert second.outcome is ImportOutcome.DUPLICATE
    assert second.video.id == first.video.id
    assert len([p for p in settings.originals_dir.rglob("*") if p.is_file()]) == 1


def test_remuxed_file_is_distinct_content(real_ingest: IngestService, user_copy: Path) -> None:
    first = real_ingest.import_video(user_copy / "clip.mp4")
    second = real_ingest.import_video(user_copy / "clip_remux.mkv")
    assert second.outcome is ImportOutcome.IMPORTED
    assert second.video.sha256 != first.video.sha256


def test_unicode_and_space_paths_import(real_ingest: IngestService, user_copy: Path) -> None:
    folder = user_copy / "Café Señor 東京 🎬"
    folder.mkdir()
    target = folder / "Episode 01 — final cut.mp4"
    shutil.copyfile(user_copy / "clip.mp4", target)
    result = real_ingest.import_video(target)
    assert result.outcome is ImportOutcome.IMPORTED
    assert result.video.original_filename == "Episode 01 — final cut.mp4"
    assert [s.source_path for s in real_ingest.repo.sources(result.video.id)] == [str(target)]


def test_video_without_audio_is_accepted(real_ingest: IngestService, user_copy: Path) -> None:
    result = real_ingest.import_video(user_copy / "silent.mov")
    types = [s.codec_type for s in real_ingest.repo.streams(result.video.id)]
    assert types == ["video"]


@pytest.mark.parametrize("name", ["audio_only.mp4", "text.mp4", "truncated.mp4"])
def test_non_video_inputs_are_rejected_before_copy(
    real_ingest: IngestService, user_copy: Path, settings: Settings, name: str
) -> None:
    with pytest.raises(UnsupportedMediaError):
        real_ingest.import_video(user_copy / name)
    assert not any(p.is_file() for p in settings.originals_dir.rglob("*"))


def test_worker_processes_queue_with_real_ffprobe(
    settings: Settings, db_conn: sqlite3.Connection, user_copy: Path
) -> None:
    queue = JobQueue(db_conn)
    ids = queue.enqueue_imports([user_copy / "clip.mp4", user_copy / "text.mp4"])
    worker = Worker(settings, worker_id="it-worker", heartbeat_interval_s=0.05)
    try:
        assert worker.run(once=True) == 2
    finally:
        worker.close()
    jobs = {job_id: queue.get(job_id) for job_id in ids}
    statuses = [job.status for job in jobs.values() if job is not None]
    assert statuses == [JobStatus.SUCCEEDED, JobStatus.FAILED]


def test_cli_import_list_check_reconcile(
    settings: Settings,
    user_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    for junk in ("text.mp4", "truncated.mp4", "audio_only.mp4"):
        (user_copy / junk).unlink()

    assert cli.main(["import", str(user_copy)]) == 0
    out = capsys.readouterr().out
    assert out.count("imported") == 3

    assert cli.main(["import", str(user_copy / "clip.mp4")]) == 0
    assert "duplicate" in capsys.readouterr().out

    assert cli.main(["list"]) == 0
    assert "clip.mp4" in capsys.readouterr().out

    assert cli.main(["show", "1", "--verify"]) == 0
    shown = capsys.readouterr().out
    assert "integrity   OK" in shown
    assert "h264" in shown

    assert cli.main(["check"]) == 0
    assert "schema v10" in capsys.readouterr().out

    assert cli.main(["reconcile"]) == 0
    assert "Adopted 0 file(s)." in capsys.readouterr().out


def test_cli_import_reports_failures(
    settings: Settings,
    user_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("IFTYEDITS_HOME", str(tmp_path))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    assert cli.main(["import", str(user_copy / "text.mp4")]) == 1
    assert "Not a readable media file" in capsys.readouterr().err
