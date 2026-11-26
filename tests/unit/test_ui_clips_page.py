"""Clips page and clip columns in Library/Overview (headless AppTest)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.core.clip_models import ClipDecision
from iftyedits.core.models import JobStatus, Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
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


def _run(page: str) -> AppTest:
    app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _button(app: AppTest, label: str) -> Any:
    return next(b for b in app.button if b.label == label)


def _analysed(conn: sqlite3.Connection, video: Video) -> None:
    transcript_id = save_transcript(conn, video, podcast())
    ClipRepository(conn).save(new_analysis(video.sha256, video.id, transcript_id))


def test_no_videos(app_env: None) -> None:
    app = _run("clips.py")
    assert any("No videos imported yet" in i.value for i in app.info)


def test_video_without_transcript_blocks_analysis(app_env: None, video: Video) -> None:
    app = _run("clips.py")
    assert any("no transcript yet" in w.value for w in app.warning)
    assert not [b for b in app.button if b.label == "Find clips"]


def test_find_clips_queues_one_job(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    save_transcript(db_conn, video, podcast())
    app = _run("clips.py")
    assert any("No suggestions yet" in i.value for i in app.info)
    _button(app, "Find clips").click()
    app.run()
    assert any("Queued analysis job" in s.value for s in app.success)
    job = JobQueue(db_conn).active_clip_analysis(video.id)
    assert job is not None and job.status is JobStatus.QUEUED
    assert _button(app, "Find clips").disabled


def test_results_filters_and_decisions_persist(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    _analysed(db_conn, video)
    app = _run("clips.py")
    table = app.dataframe[0].value
    assert len(table) >= 2 and table["Rank"].tolist()[0] == 1
    assert any(h.value.startswith("### ") for h in app.markdown)
    _button(app, "★ Favorite").click()
    app.run()
    assert not app.exception
    repo = ClipRepository(db_conn)
    current = repo.current(video.id)
    assert current is not None
    decisions = [c.decision for c in repo.candidates(current.id)]
    assert decisions[0] is ClipDecision.FAVORITE
    _button(app, "Clear decision").click()
    app.run()
    assert repo.candidates(current.id)[0].decision is None
    # An impossible minimum score shows the empty-filter message, not a crash.
    app.slider(key="clips-min-score").set_value(100).run()
    assert not app.exception


def test_library_and_overview_show_clip_state(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    assert _run("library.py").dataframe[0].value["Clips"].tolist() == ["· not started"]
    _analysed(db_conn, video)
    clips = _run("library.py").dataframe[0].value["Clips"].tolist()
    assert clips[0].startswith("✅") and "suggestions" in clips[0]
    overview = _run("overview.py")
    assert "Clips analysed" in [m.label for m in overview.metric]
