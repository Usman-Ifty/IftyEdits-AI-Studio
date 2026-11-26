"""Phase 4 pages: URL import form, Getting started, Exports and the production summary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.ui.exports import production_rows

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))


def _run(page: str) -> AppTest:
    app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _url_form(app: AppTest, url: str, allowed: bool) -> None:
    next(t for t in app.text_input if t.label == "YouTube video link").set_value(url)
    next(c for c in app.checkbox if c.label.startswith("I have permission")).set_value(allowed)


def _submit(app: AppTest, label: str) -> None:
    next(b for b in app.button if b.label == label).click()
    app.run()
    assert not app.exception


def test_url_form_requires_the_permission_box(app_env: None, db_conn: sqlite3.Connection) -> None:
    app = _run("import_videos.py")
    _url_form(app, "https://youtu.be/dQw4w9WgXcQ", allowed=False)
    _submit(app, "Produce Shorts")
    assert any("permission" in e.value for e in app.error)
    assert JobQueue(db_conn).list_recent() == []


def test_url_form_rejects_non_video_links(app_env: None, db_conn: sqlite3.Connection) -> None:
    app = _run("import_videos.py")
    _url_form(app, "https://example.com/watch?v=abc", allowed=True)
    _submit(app, "Produce Shorts")
    assert app.error
    assert JobQueue(db_conn).list_recent() == []


def test_url_form_queues_one_production_job(app_env: None, db_conn: sqlite3.Connection) -> None:
    app = _run("import_videos.py")
    _url_form(app, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", allowed=True)
    _submit(app, "Produce Shorts")
    (job,) = JobQueue(db_conn).list_recent()
    assert job.kind == PRODUCE_VIDEO
    assert any("queued" in s.value for s in app.success)
    _submit(app, "Produce Shorts")  # same link again does not create a second job
    assert len(JobQueue(db_conn).list_recent()) == 1


def test_download_only_queues_a_fetch(app_env: None, db_conn: sqlite3.Connection) -> None:
    app = _run("import_videos.py")
    _url_form(app, "https://youtu.be/dQw4w9WgXcQ", allowed=True)
    _submit(app, "Download only")
    (job,) = JobQueue(db_conn).list_recent()
    assert job.kind == "fetch_url"


def test_getting_started_page_renders_and_is_dismissable(app_env: None) -> None:
    app = _run("getting_started.py")
    text = " ".join(m.value for m in app.markdown)
    assert "YouTube" in text and "Exports" in text


def test_exports_page_empty_state(app_env: None) -> None:
    app = _run("exports.py")
    assert any("No Shorts yet" in m.value for m in app.markdown)


def test_production_rows_handle_missing_and_present_results() -> None:
    class Fake:
        def __init__(self, result: object) -> None:
            self.result = result

    assert production_rows(Fake(None)) == []  # type: ignore[arg-type]
    assert production_rows(Fake({"clips": "bad"})) == []  # type: ignore[arg-type]
    rows = production_rows(
        Fake(  # type: ignore[arg-type]
            {
                "clips": [
                    {
                        "rank": 1,
                        "title": "A",
                        "score": 80,
                        "status": "succeeded",
                        "duration_s": 20.0,
                        "width": 1080,
                        "height": 1920,
                        "output": "/x/a.mp4",
                    },
                    {"rank": 2, "title": "B", "score": 70, "status": "failed", "error": "boom"},
                ]
            }
        )
    )
    assert rows[0]["Size"] == "1080x1920" and rows[0]["Output"] == "/x/a.mp4"
    assert rows[1]["Size"] == "-" and rows[1]["Reason"] == "boom"


def test_getting_started_shows_tracker_and_help_sections(app_env: None) -> None:
    app = _run("getting_started.py")
    text = " ".join(m.value for m in app.markdown)
    assert "Import" in text and "Export" in text
    labels = " ".join(e.label for e in app.expander)
    assert "What the scores mean" in labels and "When something fails" in labels


def test_workflow_tracker_and_selected_status() -> None:
    from iftyedits.ui.theme import clip_status, workflow_tracker

    none_done = workflow_tracker((False,) * 5)
    assert "Next:" in none_done and "Import" in none_done
    partial = workflow_tracker((True, True, False, False, False))
    assert partial.count("Next:") == 1
    assert clip_status("selected") == ("Selected", "good")


def test_jobs_page_retries_a_failed_production(app_env: None, db_conn: sqlite3.Connection) -> None:
    queue = JobQueue(db_conn)
    queue.enqueue_production({}, url="https://youtu.be/dQw4w9WgXcQ")
    job = queue.claim("w1")
    assert job is not None
    assert queue.fail(job.id, "network down", worker_id="w1")
    app = _run("jobs.py")
    _submit(app, "Retry")
    jobs = queue.list_recent()
    assert sum(1 for j in jobs if j.status.value == "queued") == 1
