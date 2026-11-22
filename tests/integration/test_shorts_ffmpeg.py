"""Short rendering with real FFmpeg on generated media (Phase 3)."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.config import Settings
from iftyedits.core.errors import ExternalToolError, JobCancelledError, ShortConfigError
from iftyedits.core.models import JobStatus, Video
from iftyedits.core.shorts import AudioSettings, PolishSettings, ShortConfig
from iftyedits.pipeline.jobs import RENDER_SHORT, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService, build_vault
from iftyedits.services.shorts import ShortService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.shorts import KIND_EXPORT, KIND_PREVIEW, ShortRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from tests.clip_db_fakes import save_transcript
from tests.clip_fakes import line_segment
from tests.integration.conftest import run_ffmpeg

pytestmark = pytest.mark.ffmpeg

H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


@pytest.fixture(scope="module")
def sources(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("short_sources")

    def make(name: str, size: str, *, audio: bool, seconds: int = 12, rate: int = 30) -> None:
        args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"]
            args += [*H264, "-c:a", "aac", "-shortest"]
        else:
            args += [*H264, "-an"]
        run_ffmpeg(ffmpeg, *args, str(root / name))

    make("land.mp4", "1280x720", audio=True)
    make("hd.mp4", "1920x1080", audio=True, rate=60)
    make("square.mp4", "720x720", audio=True)
    make("portrait.mp4", "720x1280", audio=True)
    make("silent.mp4", "1280x720", audio=False)
    return {p.name: p for p in root.iterdir()}


@pytest.fixture
def real_ingest(settings: Settings, db_conn: sqlite3.Connection) -> IngestService:
    return IngestService(
        settings, VideoRepository(db_conn), FFprobe(timeout_s=60), build_vault(settings)
    )


Setup = Callable[[str], tuple[ShortService, int]]


@pytest.fixture
def make_short(
    settings: Settings,
    db_conn: sqlite3.Connection,
    real_ingest: IngestService,
    sources: dict[str, Path],
) -> Iterator[Setup]:
    """Import a generated source, give it a transcript and open a project over 2 to 8 s."""

    def build(name: str) -> tuple[ShortService, int]:
        video = real_ingest.import_video(sources[name]).video
        save_transcript(
            db_conn,
            video,
            [
                line_segment(1.0, "hello there this is a test caption"),
                line_segment(4.0, "second line with Ünïcode and 日本語 text"),
            ],
        )
        queue = JobQueue(db_conn)
        service = ShortService(
            settings,
            VideoRepository(db_conn),
            TranscriptRepository(db_conn),
            ClipRepository(db_conn),
            ShortRepository(db_conn),
            queue,
            FFprobe(timeout_s=60),
            lambda: FFmpeg(settings.ffmpeg_path, settings.ffmpeg_timeout_s, encoder="libx264"),
        )
        project = _project(service, db_conn, video)
        return service, project

    yield build


def _project(service: ShortService, conn: sqlite3.Connection, video: Video) -> int:
    """A project without running clip analysis: insert through the repository."""
    config = ShortConfig(start_s=2.0, end_s=8.0)
    return service.shorts.create_project(
        video.id, f"test-{video.id}", "Test clip", 2.0, 8.0, config
    ).id


class _Cancelled:
    def is_set(self) -> bool:
        return True


def _probe(path: Path) -> dict[str, object]:
    info = FFprobe(timeout_s=60).probe(path)
    video = info.primary_video
    assert video is not None
    return {
        "w": video.width,
        "h": video.height,
        "duration": info.duration_s,
        "audio": bool(info.audio_streams),
    }


def _decode(ffmpeg: str, path: Path) -> None:
    done = subprocess.run(
        [ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr.strip() == ""


def _run(service: ShortService, kind: str = KIND_EXPORT, project: int = 1) -> Path:
    request = service.request_render(project, kind)
    assert request.created
    report = service.run_render(request.render.id)
    return report.output_path


@pytest.mark.parametrize("name", ["land.mp4", "hd.mp4", "square.mp4", "portrait.mp4"])
def test_export_is_verified_vertical_with_audio_for_every_source_shape(
    make_short: Setup, ffmpeg: str, name: str, settings: Settings
) -> None:
    service, project = make_short(name)
    out = _run(service, project=project)
    props = _probe(out)
    assert (props["w"], props["h"]) == (1080, 1920)
    assert props["audio"] is True
    assert abs(float(props["duration"]) - 6.0) < 0.3  # type: ignore[arg-type]
    _decode(ffmpeg, out)
    assert out.parent == settings.exports_dir / "shorts" / f"{Path(name).stem}-v1"
    assert out.suffix == ".mp4" and not out.name.startswith(".")
    row = service.shorts.list_renders(project)[0]
    assert row.status == "succeeded" and row.output_path == out and row.size_bytes
    assert not list((settings.exports_dir / "shorts" / ".partial").glob("*"))
    work = settings.derived_dir / "shorts" / "work"
    assert not work.exists() or not list(work.iterdir())


def test_source_without_audio_exports_silently_and_cleanly(make_short: Setup, ffmpeg: str) -> None:
    service, project = make_short("silent.mp4")
    out = _run(service, project=project)
    props = _probe(out)
    assert props["audio"] is False and (props["w"], props["h"]) == (1080, 1920)
    _decode(ffmpeg, out)


def test_preview_is_small_and_reuses_the_same_crop(make_short: Setup, ffmpeg: str) -> None:
    service, project = make_short("land.mp4")
    out = _run(service, KIND_PREVIEW, project)
    props = _probe(out)
    assert (props["w"], props["h"]) == (540, 960)
    assert "previews" in out.parts
    _decode(ffmpeg, out)
    again = service.request_render(project, KIND_PREVIEW)
    assert not again.created and again.render.output_path == out


def test_polish_and_audio_settings_render(make_short: Setup, ffmpeg: str) -> None:
    service, project = make_short("hd.mp4")
    config = ShortConfig(
        start_s=2.0,
        end_s=8.0,
        crop_position=0.2,
        audio=AudioSettings(volume_percent=150, fade_in_s=0.5, fade_out_s=0.5, normalize=True),
        polish=PolishSettings(fade_in_s=0.4, fade_out_s=0.4, brightness=0.05, contrast=1.1),
        text_overrides={0: "Corrected caption"},
    )
    service.save_config(project, config)
    out = _run(service, project=project)
    _decode(ffmpeg, out)
    assert _probe(out)["audio"] is True


def test_captions_are_actually_drawn(make_short: Setup, ffmpeg: str, tmp_path: Path) -> None:
    service, project = make_short("land.mp4")
    base = service.project(project).config
    from dataclasses import replace

    plain = replace(base, captions=replace(base.captions, enabled=False))
    service.save_config(project, plain)
    off = _run(service, project=project)
    service.save_config(project, base)
    on = _run(service, project=project)
    frame_off, frame_on = tmp_path / "off.raw", tmp_path / "on.raw"
    for src, dst in ((off, frame_off), (on, frame_on)):
        run_ffmpeg(
            ffmpeg, "-ss", "1.5", "-i", str(src), "-frames:v", "1", "-f", "rawvideo",
            "-pix_fmt", "gray", str(dst),
        )  # fmt: skip
    assert frame_off.read_bytes() != frame_on.read_bytes()
    assert frame_on.stat().st_size == frame_off.stat().st_size == 1080 * 1920


def test_second_export_never_overwrites_the_first(make_short: Setup) -> None:
    service, project = make_short("land.mp4")
    first = _run(service, project=project)
    before = first.read_bytes()
    same = service.request_render(project, KIND_EXPORT)
    assert not same.created and same.render.output_path == first
    forced = service.request_render(project, KIND_EXPORT, force=True)
    assert forced.created
    second = service.run_render(forced.render.id).output_path
    assert second != first and second.name.endswith("-2.mp4")
    assert first.read_bytes() == before


def test_stale_collision_gets_a_new_name(make_short: Setup, settings: Settings) -> None:
    service, project = make_short("land.mp4")
    request = service.request_render(project, KIND_EXPORT)
    from iftyedits.services.shorts import export_stem

    video = service.videos.get(service.project(project).video_id)
    assert video is not None
    stem = export_stem(video, service.project(project).config, request.render.config_hash)
    squatter = settings.exports_dir / "shorts" / "land-v1" / f"{stem}.mp4"
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_bytes(b"someone else's file")
    out = service.run_render(request.render.id).output_path
    assert out != squatter and squatter.read_bytes() == b"someone else's file"


def test_failed_encode_leaves_no_partial_and_marks_the_row(
    make_short: Setup, settings: Settings
) -> None:
    service, project = make_short("land.mp4")

    class Broken(FFmpeg):
        def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
            partial = next(Path(a) for a in args if a.endswith(".partial"))
            partial.write_bytes(b"half a file")
            raise ExternalToolError("ffmpeg exploded")

    service.transcoder = lambda: Broken(settings.ffmpeg_path, 30, encoder="libx264")
    request = service.request_render(project, KIND_EXPORT)
    with pytest.raises(ExternalToolError):
        service.run_render(request.render.id)
    row = service.shorts.get_render(request.render.id)
    assert row is not None and row.status == "failed" and row.error
    assert row.output_path is None
    assert not list((settings.exports_dir / "shorts").rglob("*.mp4"))
    assert not list((settings.exports_dir / "shorts" / ".partial").glob("*"))
    retry = service.request_render(project, KIND_EXPORT)
    assert retry.created and retry.render.id != request.render.id


def test_verification_failure_is_not_recorded_as_success(
    make_short: Setup, settings: Settings
) -> None:
    service, project = make_short("land.mp4")

    class Truncating(FFmpeg):
        def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
            partial = next(Path(a) for a in args if a.endswith(".partial"))
            partial.write_bytes(b"not a video at all")

    service.transcoder = lambda: Truncating(settings.ffmpeg_path, 30, encoder="libx264")
    request = service.request_render(project, KIND_EXPORT)
    with pytest.raises(Exception):  # noqa: B017 - any verification error type
        service.run_render(request.render.id)
    row = service.shorts.get_render(request.render.id)
    assert row is not None and row.status == "failed"
    assert not list((settings.exports_dir / "shorts").rglob("*.mp4"))
    assert not list((settings.exports_dir / "shorts" / ".partial").glob("*"))


def test_cancel_mid_render_cleans_up(make_short: Setup, settings: Settings) -> None:
    service, project = make_short("land.mp4")
    request = service.request_render(project, KIND_EXPORT)
    with pytest.raises(JobCancelledError):
        service.run_render(request.render.id, cancel=_Cancelled())
    row = service.shorts.get_render(request.render.id)
    assert row is not None and row.status == "cancelled"
    assert not list((settings.exports_dir / "shorts").rglob("*.mp4"))


def test_out_of_range_request_is_rejected_before_queueing(make_short: Setup) -> None:
    service, project = make_short("land.mp4")
    with pytest.raises(ShortConfigError):
        service.save_config(project, ShortConfig(start_s=5.0, end_s=500.0))
    assert service.shorts.list_renders(project) == []


def test_worker_runs_a_render_job_with_progress_and_dedupes(
    make_short: Setup, settings: Settings, db_conn: sqlite3.Connection, ffmpeg: str
) -> None:
    service, project = make_short("land.mp4")
    first = service.request_render(project, KIND_EXPORT)
    second = service.request_render(project, KIND_EXPORT)
    assert first.created and not second.created and second.render.id == first.render.id
    worker = Worker(settings, worker_id="w-short", heartbeat_interval_s=0.05, poll_interval_s=0.01)
    try:
        assert worker.run(once=True) == 1
    finally:
        worker.close()
    queue = JobQueue(db_conn)
    job = queue.get(first.render.job_id or 0)
    assert job is not None and job.kind == RENDER_SHORT and job.status is JobStatus.SUCCEEDED
    assert job.result is not None and job.result["width"] == 1080
    row = ShortRepository(db_conn).get_render(first.render.id)
    assert row is not None and row.status == "succeeded" and row.output_path is not None
    _decode(ffmpeg, row.output_path)


def test_original_video_is_untouched(make_short: Setup, sources: dict[str, Path]) -> None:
    from iftyedits.storage.originals import sha256_file

    before = sha256_file(sources["land.mp4"])
    service, project = make_short("land.mp4")
    _run(service, project=project)
    assert sha256_file(sources["land.mp4"]) == before
