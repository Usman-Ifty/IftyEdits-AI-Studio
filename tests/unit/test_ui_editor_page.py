"""Short editor page (headless AppTest): selection, state isolation, queueing, errors."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import RENDER_SHORT
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.shorts import ShortRepository
from tests.clip_db_fakes import save_transcript
from tests.clip_fakes import new_analysis, podcast
from tests.conftest import MakeMedia

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))
    yield


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Show.mp4")).video


def _analyse(conn: sqlite3.Connection, video: Video) -> None:
    conn.execute("UPDATE videos SET duration_s = 400 WHERE id = ?", (video.id,))
    conn.commit()
    transcript_id = save_transcript(conn, video, podcast())
    ClipRepository(conn).save(new_analysis(video.sha256, video.id, transcript_id))


def _run() -> AppTest:
    app = AppTest.from_file(str(APP / "views" / "editor.py"), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _button(app: AppTest, label: str) -> Any:
    return next(b for b in app.button if b.label == label)


def test_no_suggestions_shows_guidance(app_env: None) -> None:
    app = _run()
    assert any("No clip suggestions yet" in i.value for i in app.info)


def test_page_opens_a_project_and_leaves_the_suggestion_unchanged(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    _analyse(db_conn, video)
    candidates = ClipRepository(db_conn).candidates(
        ClipRepository(db_conn).current(video.id).id  # type: ignore[union-attr]
    )
    before = [(c.start_s, c.end_s) for c in candidates]
    app = _run()
    projects = ShortRepository(db_conn).list_projects(video.id)
    assert len(projects) == 1 and projects[0].candidate_key == candidates[0].key
    assert [b.label for b in app.button if b.label == "Export Short"]
    after = ClipRepository(db_conn).candidates(candidates[0].analysis_id)
    assert [(c.start_s, c.end_s) for c in after] == before


def test_export_button_queues_exactly_one_job_even_when_pressed_twice(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    _analyse(db_conn, video)
    app = _run()
    _button(app, "Export Short").click()
    app.run()
    _button(app, "Export Short").click()
    app.run()
    jobs = db_conn.execute("SELECT count(*) FROM jobs WHERE kind = ?", (RENDER_SHORT,)).fetchone()
    assert jobs[0] == 1
    renders = db_conn.execute("SELECT status, kind FROM short_renders").fetchall()
    assert [tuple(r) for r in renders] == [("queued", "export")]


def test_other_pages_still_render_after_editor_added(app_env: None, video: Video) -> None:
    for page in ("overview.py", "library.py", "clips.py", "jobs.py"):
        app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
        app.run()
        assert not app.exception, (page, [e.value for e in app.exception])
