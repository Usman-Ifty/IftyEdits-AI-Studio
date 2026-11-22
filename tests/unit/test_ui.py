"""Streamlit pages (headless AppTest) and formatting helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.core.models import Job, JobStatus, StreamInfo
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.db import open_database
from iftyedits.ui.formatting import human_duration, human_size, job_row, resolution, stream_row
from tests.conftest import MakeMedia, path_input, scan_button

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    yield


def _run(page: str) -> AppTest:
    app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KB"), (5 * 1024**3, "5.0 GB")],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "-"), (5.4, "0:05"), (125, "2:05"), (3725, "1:02:05")]
)
def test_human_duration(seconds: float | None, text: str) -> None:
    assert human_duration(seconds) == text


def test_resolution_uses_rotation_and_skips_cover_art() -> None:
    streams = [
        StreamInfo(0, "video", "mjpeg", width=500, height=500, is_attached_pic=True),
        StreamInfo(1, "video", "hevc", width=1920, height=1080, rotation=90),
    ]
    assert resolution(streams) == "1080x1920"
    assert resolution([]) == "-"


def test_stream_row_flags() -> None:
    row = stream_row(
        StreamInfo(
            0, "video", "hevc", color_transfer="smpte2084", avg_frame_rate=29.0, r_frame_rate=60.0
        )
    )
    assert row["Flags"] == "HDR, VFR"


def test_job_row_shows_error_first() -> None:
    job = Job(
        1, "import_file", {"path": "/a/b/clip.mp4"}, JobStatus.FAILED, 0.3, "copying", "boom",
        None, 1, "2026-11-18T10:00:00.000000+00:00", None, None, None, None,
    )  # fmt: skip
    row = job_row(job)
    assert (row["File"], row["Detail"], row["Created (UTC)"]) == (
        "clip.mp4",
        "boom",
        "2026-11-18 10:00:00",
    )


# ------------------------------------------------------------------ pages


def test_overview_page_without_worker(app_env: None) -> None:
    app = _run("overview.py")
    assert any("Dashboard" in m.value for m in app.markdown)
    assert any("No background worker" in w.value for w in app.warning)


def test_library_page_empty(app_env: None) -> None:
    app = _run("library.py")
    assert any("No videos yet" in i.value for i in app.info)


def test_library_page_lists_imported_video(
    app_env: None, ingest: IngestService, make_media: MakeMedia
) -> None:
    ingest.import_video(make_media("Podcast 7.mp4"))
    app = _run("library.py")
    assert app.subheader[0].value == "Podcast 7.mp4"
    assert app.selectbox[0].options == ["#1  Podcast 7.mp4"]


def test_import_page_scans_folder_and_queues_without_importing(
    app_env: None, settings: Settings, make_media: MakeMedia
) -> None:
    first = make_media("ep1.mp4")
    make_media("ep2.mov")
    app = _run("import_videos.py")
    path_input(app).set_value(str(first.parent))
    scan_button(app).click()  # form submit: Scan
    app.run()
    assert not app.exception
    queue_button = next(b for b in app.button if b.label.startswith("Queue 2"))
    queue_button.click()
    app.run()
    assert any("Queued 2 import(s)" in s.value for s in app.success)

    conn = open_database(settings.db_path)
    try:
        jobs = JobQueue(conn).list_recent()
    finally:
        conn.close()
    assert {j.status for j in jobs} == {JobStatus.QUEUED}
    assert not any(p.is_file() for p in settings.originals_dir.rglob("*"))


def test_import_page_rejects_data_directory(app_env: None, settings: Settings) -> None:
    app = _run("import_videos.py")
    path_input(app).set_value(str(settings.data_dir))
    scan_button(app).click()
    app.run()
    assert any("data directory" in e.value for e in app.error)


def test_import_page_reports_missing_path(app_env: None, tmp_path: Path) -> None:
    app = _run("import_videos.py")
    path_input(app).set_value(str(tmp_path / "missing.mp4"))
    scan_button(app).click()
    app.run()
    assert any("not found" in e.value for e in app.error)


def test_library_marks_duplicate_imports(
    app_env: None, ingest: IngestService, make_media: MakeMedia
) -> None:
    source = make_media("talk.mp4")
    ingest.import_video(source)
    ingest.import_video(make_media("talk copy.MOV", kind=b"", body=source.read_bytes()))
    app = _run("library.py")
    assert any("imported from 2 locations" in i.value for i in app.info)
    assert app.dataframe[0].value["Sources"].tolist() == [2]


def test_import_page_lists_skipped_items(app_env: None, make_media: MakeMedia) -> None:
    folder = make_media("a.mp4").parent
    make_media("notes.txt")
    make_media("sub/b.mp4")
    app = _run("import_videos.py")
    path_input(app).set_value(str(folder))
    scan_button(app).click()
    app.run()
    assert any("2 item(s) in this folder will not be imported" in w.value for w in app.warning)
    table = next(d.value for d in app.dataframe if "Reason" in d.value.columns)
    reasons = table["Reason"].tolist()
    assert sorted(reasons) == sorted(
        ["subfolder (enable recursive import to include)", "unsupported file extension"]
    )


def test_job_row_shows_outcome() -> None:
    job = Job(
        2, "import_file", {"path": "/x/a.mp4"}, JobStatus.SUCCEEDED, 1.0, "duplicate: a.mp4",
        None, {"outcome": "duplicate"}, 1, "2026-11-18T10:00:00.000000+00:00", None, None,
        None, None,
    )  # fmt: skip
    assert job_row(job)["Outcome"] == "duplicate"
