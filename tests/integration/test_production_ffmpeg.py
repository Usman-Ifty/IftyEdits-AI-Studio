"""One-click production end to end: fake download and speech, real FFmpeg for everything else."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.core.protocols import SourceDownloader, SpeechModelStore, SpeechTranscriber
from iftyedits.core.sources import SourceMetadata, VideoUrl
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.production import ProductionOptions
from tests.clip_fakes import podcast
from tests.integration.conftest import FFPROBE, run_ffmpeg
from tests.transcription_fakes import FakeModelStore, FakeTranscriber

pytestmark = [pytest.mark.ffmpeg, pytest.mark.slow]

URL = "https://www.youtube.com/watch?v=abcdefghijk"
LENGTH_S = 235


class FakeDownloader:
    """Copies a generated clip instead of touching the network."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.downloads = 0

    def fetch_metadata(self, url: VideoUrl) -> SourceMetadata:
        return SourceMetadata(
            source_key=url.source_key,
            url=url.canonical,
            title="Podcast Night",
            uploader="Me",
            duration_s=float(LENGTH_S),
            upload_date=None,
            license=None,
            is_live=False,
        )

    def download(
        self,
        url: VideoUrl,
        directory: Path,
        *,
        progress: object = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        self.downloads += 1
        target = directory / "download.mp4"
        shutil.copy2(self.source, target)
        return target


@pytest.fixture(scope="module")
def long_video(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("long") / "podcast.mp4"
    run_ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=10:duration={LENGTH_S}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={LENGTH_S}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def _worker(settings: Settings, downloader: SourceDownloader, engine: FakeTranscriber) -> Worker:
    def transcriber(_: Settings) -> SpeechTranscriber:
        return engine

    def store(_: Settings) -> SpeechModelStore:
        return FakeModelStore(settings.models_dir)

    return Worker(
        settings,
        transcriber_factory=transcriber,
        model_store_factory=store,
        downloader_factory=lambda _: downloader,
        worker_id="w-prod",
        heartbeat_interval_s=0.05,
        poll_interval_s=0.01,
    )


def _probe(path: Path) -> dict[str, object]:
    assert FFPROBE is not None
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )  # fmt: skip
    data: dict[str, object] = json.loads(out.stdout)
    return data


def test_url_to_separate_vertical_shorts_and_idempotent_retry(
    settings: Settings, db_conn: sqlite3.Connection, long_video: Path
) -> None:
    engine = FakeTranscriber(podcast(), duration=float(LENGTH_S))
    downloader = FakeDownloader(long_video)
    worker = _worker(settings, downloader, engine)
    try:
        queue = JobQueue(db_conn)
        options = ProductionOptions(max_clips=2, min_score=0.0)
        job_id, created = queue.enqueue_production(options.as_payload(), url=URL)
        assert created
        worker.run(once=True)
        job = queue.get(job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED, job.error
        result = job.result or {}
        assert result["succeeded"] >= 1
        assert result["failed"] == 0
        outputs = [Path(str(c["output"])) for c in result["clips"] if c["status"] == "succeeded"]
        assert len(set(outputs)) == len(outputs)
        for path in outputs:
            assert path.is_file() and path.suffix == ".mp4"
            assert settings.exports_dir in path.parents
            info = _probe(path)
            streams = info["streams"]
            assert isinstance(streams, list)
            video = next(s for s in streams if s["codec_type"] == "video")
            assert (video["width"], video["height"]) == (1080, 1920)
            assert any(s["codec_type"] == "audio" for s in streams)
        assert downloader.downloads == 1
        assert len(engine.calls) == 1

        # A second production of the same link reuses everything and re-exports nothing new.
        before = {p: p.stat().st_mtime_ns for p in outputs}
        again, created_again = queue.enqueue_production(options.as_payload(), url=URL)
        assert created_again and again != job_id
        worker.run(once=True)
        second = queue.get(again)
        assert second is not None
        assert second.status is JobStatus.SUCCEEDED, second.error
        assert downloader.downloads == 1
        assert len(engine.calls) == 1
        for path, stamp in before.items():
            assert path.stat().st_mtime_ns == stamp
    finally:
        worker.close()


def test_cancelled_production_keeps_the_original(
    settings: Settings, db_conn: sqlite3.Connection, long_video: Path
) -> None:
    engine = FakeTranscriber(podcast(), behaviour="wait_for_cancel", duration=float(LENGTH_S))
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_production(ProductionOptions().as_payload(), url=URL)

    def run() -> None:
        worker = _worker(settings, FakeDownloader(long_video), engine)  # own thread, own DB
        try:
            worker.process_one()
        finally:
            worker.close()

    runner = threading.Thread(target=run)
    runner.start()
    assert engine.started.wait(120)
    assert queue.request_cancel(job_id).value == "requested"
    runner.join(60)
    assert not runner.is_alive()
    job = queue.get(job_id)
    assert job is not None and job.status is JobStatus.CANCELLED
    assert len(list(settings.originals_dir.rglob("*.mp4"))) == 1
    assert not list(settings.exports_dir.rglob("*.mp4"))
