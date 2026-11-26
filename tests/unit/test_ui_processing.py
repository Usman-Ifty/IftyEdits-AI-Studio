"""Streamlit pages for media preparation and jobs (headless AppTest, no FFmpeg)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.core.models import (
    Artifact,
    ArtifactKind,
    Job,
    JobStatus,
    Stage,
    StageRecord,
    StageStatus,
    Video,
)
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.ui.formatting import (
    artifact_row,
    job_row,
    processing_columns,
    stage_cell,
    stage_row,
)
from tests.conftest import MakeMedia, path_input, scan_button

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    yield


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Episode 4.mp4")).video


def _run(page: str) -> AppTest:
    app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _button(app: AppTest, label: str) -> Any:
    return next(b for b in app.button if b.label == label)


def _prepared(settings: Settings, conn: sqlite3.Connection, video: Video) -> None:
    """Record finished stages and a real JPEG thumbnail, as the worker would."""
    repo = ProcessingRepository(conn)
    folder = settings.derived_dir / video.sha256[:2] / video.sha256
    folder.mkdir(parents=True)
    thumb = folder / "thumbnail-abc.jpg"
    Image.new("RGB", (640, 360), (200, 40, 40)).save(thumb, "JPEG")
    for stage in (Stage.AUDIO, Stage.PROXY):
        record = repo.start_stage(video.id, stage, params_hash="p", params={})
        repo.finish_stage(record.id, StageStatus.SUCCEEDED, message=f"{stage.value} ok")
    record = repo.start_stage(video.id, Stage.THUMBNAIL, params_hash="p", params={})
    repo.register_artifact(
        video_id=video.id,
        stage_id=record.id,
        kind=ArtifactKind.THUMBNAIL_JPG,
        relpath=thumb.relative_to(settings.derived_dir).as_posix(),
        size_bytes=thumb.stat().st_size,
        sha256=sha256_file(thumb),
        params_hash="p",
        metadata={"width": 640, "height": 360, "input": "proxy"},
    )
    repo.finish_stage(record.id, StageStatus.SUCCEEDED, message="thumb ok")


# ------------------------------------------------------------------ formatting


def test_stage_and_job_formatting() -> None:
    assert stage_cell(None) == "· not started"
    assert stage_cell(StageStatus.SUCCEEDED) == "✅ done"
    assert processing_columns({Stage.PROXY: StageStatus.FAILED}) == {
        "Audio": "· not started",
        "Proxy": "❌ failed",
        "Thumbnail": "· not started",
    }
    record = StageRecord(
        1, 1, Stage.PROXY, StageStatus.FAILED, 3, "p", {}, 0.4, None, "ffmpeg failed",
        "2026-11-20T10:00:00.000000+00:00", None,
    )  # fmt: skip
    row = stage_row(record)
    assert (row["Stage"], row["Detail"], row["Finished (UTC)"]) == ("Proxy", "ffmpeg failed", "")

    job = Job(
        5, "process_video", {"video_id": 9, "stages": ["proxy"]}, JobStatus.RUNNING, 0.5,
        "proxy: 50%", None, None, 1, "2026-11-20T10:00:00.000000+00:00", None, None, None,
        "w", "2026-11-20T10:01:00.000000+00:00",
    )  # fmt: skip
    row = job_row(job)
    assert (row["Kind"], row["File"], row["Status"]) == ("prepare media", "video #9", "cancelling")

    done = Job(
        6, "process_video", {"video_id": 9}, JobStatus.SUCCEEDED, 1.0, None, None,
        {"stages": {"audio": "skipped", "proxy": "succeeded", "thumbnail": "succeeded"}}, 1,
        "2026-11-20T10:00:00.000000+00:00", None, None, None, None,
    )  # fmt: skip
    assert job_row(done)["Outcome"] == "3/3 stages"

    proxy = Artifact(
        1, 9, 1, 5, ArtifactKind.PROXY_MP4, "ab/x/proxy.mp4", 2048, "f" * 64, "p",
        {"width": 1280, "height": 720, "fps": 30.0, "has_audio": True,
         "hardware_encoded": True, "encoder": "h264_videotoolbox", "duration_s": 65.0},
        "2026-11-20T10:00:00.000000+00:00",
    )  # fmt: skip
    details = artifact_row(proxy)["Details"]
    assert "1280x720" in details and "VideoToolbox (hardware)" in details and "1:05" in details


# ------------------------------------------------------------------ library


def test_library_shows_unprepared_video_and_queues_preparation(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    app = _run("library.py")
    table = app.dataframe[0].value
    assert table["Audio"].tolist() == ["· not started"]
    assert any("not been prepared" in c.value for c in app.caption)
    assert any("No thumbnail yet" in c.value for c in app.caption)

    _button(app, "Prepare media").click()
    app.run()
    assert not app.exception
    assert any("Queued job" in s.value for s in app.success)
    job = JobQueue(db_conn).active_processing(video.id)
    assert job is not None and job.payload["force"] is False


def test_library_shows_thumbnail_stages_and_artifacts(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _prepared(settings, db_conn, video)
    app = _run("library.py")
    assert app.dataframe[0].value["Thumbnail"].tolist() == ["✅ done"]
    assert len(app.image) == 1
    assert any("Audio, proxy and thumbnail are ready" in s.value for s in app.success)
    stage_table = next(d.value for d in app.dataframe if "Stage" in d.value.columns)
    assert stage_table["Stage"].tolist() == ["Audio", "Proxy", "Thumbnail"]
    artifact_table = next(d.value for d in app.dataframe if "Kind" in d.value.columns)
    assert artifact_table["Kind"].tolist() == ["thumbnail_jpg"]

    _button(app, "Regenerate all").click()
    app.run()
    job = JobQueue(db_conn).active_processing(video.id)
    assert job is not None and job.payload["force"] is True


def test_library_shows_stage_errors(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    repo = ProcessingRepository(db_conn)
    record = repo.start_stage(video.id, Stage.PROXY, params_hash="p", params={})
    repo.finish_stage(record.id, StageStatus.FAILED, error="ffmpeg failed (exit 1): bad data")
    app = _run("library.py")
    assert any("proxy: ffmpeg failed (exit 1): bad data" in e.value for e in app.error)


def test_library_cancel_button_for_active_job(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    job_id, _ = JobQueue(db_conn).enqueue_processing(video.id, [Stage.PROXY])
    app = _run("library.py")
    prepare = _button(app, "Prepare media")
    assert prepare.disabled
    assert any("No worker is running" in w.value for w in app.warning)
    _button(app, "Cancel processing").click()
    app.run()
    assert any("Cancelled before it started" in s.value for s in app.success)
    job = JobQueue(db_conn).get(job_id)
    assert job is not None and job.status is JobStatus.CANCELLED


# ------------------------------------------------------------------ jobs page


def test_jobs_page_empty(app_env: None) -> None:
    app = _run("jobs.py")
    assert any("Nothing is running or queued" in c.value for c in app.caption)
    assert any("No finished jobs yet" in c.value for c in app.caption)


def test_jobs_page_cancels_running_job_and_lists_errors(
    app_env: None, db_conn: sqlite3.Connection
) -> None:
    queue = JobQueue(db_conn)
    running, _ = queue.enqueue_processing(1, [Stage.PROXY])
    failed, _ = queue.enqueue_processing(2, [Stage.AUDIO])
    queue.claim("w")
    queue.heartbeat(running, 0.42, "proxy: 42%", worker_id="w")
    queue.claim("w")
    queue.fail(failed, "audio: ffmpeg failed", worker_id="w")

    app = _run("jobs.py")
    assert any("Job " in m.value and "video #1" in m.value for m in app.markdown)
    _button(app, "Cancel").click()
    app.run()
    assert not app.exception
    assert any("Cancellation of job" in i.value for i in app.info)
    assert queue.cancel_requested(running)
    assert any("audio: ffmpeg failed" in e.value for e in app.error)

    app = _run("jobs.py")
    assert any("waiting for the worker to stop" in c.value for c in app.caption)
    assert not any(b.label == "Cancel" for b in app.button)


def test_import_page_requests_preparation_by_default(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, make_media: MakeMedia
) -> None:
    clip = make_media("ep9.mp4")
    app = _run("import_videos.py")
    path_input(app).set_value(str(clip))
    scan_button(app).click()
    app.run()
    prepare = next(c for c in app.checkbox if c.label.startswith("Prepare media"))
    assert prepare.value is True
    next(b for b in app.button if b.label.startswith("Queue 1")).click()
    app.run()
    (job,) = JobQueue(db_conn).list_recent()
    assert job.payload == {"path": str(clip), "process": True}


def test_overview_counts_prepared_videos(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    app = _run("overview.py")
    assert any(m.label == "Media prepared" and m.value == "0 / 1" for m in app.metric)
    assert any("still need media preparation" in i.value for i in app.info)
    _prepared(settings, db_conn, video)
    app = _run("overview.py")
    assert any(m.label == "Media prepared" and m.value == "1 / 1" for m in app.metric)
