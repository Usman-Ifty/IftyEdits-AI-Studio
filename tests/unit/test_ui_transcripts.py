"""Transcripts page and transcript columns in Library/Overview (headless AppTest)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.adapters.model_store import CATALOG, build_model_store
from iftyedits.config import Settings
from iftyedits.core.models import JobStatus, ModelState, Video
from iftyedits.core.transcript import normalize_segments
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from iftyedits.ui.formatting import (
    escape_markdown,
    highlight_snippet,
    human_size,
    job_outcome,
    job_target,
    language_label,
    timestamp,
    transcript_cell,
)
from tests.conftest import MakeMedia
from tests.transcription_fakes import DEFAULT_SEGMENTS

APP = Path(__file__).resolve().parents[2] / "app"
SMALL = next(spec for spec in CATALOG if spec.name == "small")
DOWNLOAD_SMALL = f"Download small ({human_size(SMALL.size_bytes)})"


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
    return ingest.import_video(make_media("Gaming Stream.mp4")).video


def _install(settings: Settings, name: str) -> None:
    """Fake an installed model with sparse files of the right sizes (no download)."""
    store = build_model_store(settings)
    folder = store.path(name)
    folder.mkdir(parents=True)
    for file in store.spec(name).files:
        with (folder / file.name).open("wb") as handle:
            handle.truncate(file.size_bytes)
    assert store.status(name).state is ModelState.INSTALLED


def _save(conn: sqlite3.Connection, video_id: int, **changes: Any) -> None:
    values: dict[str, Any] = {
        "video_id": video_id,
        "audio_sha256": "d" * 64,
        "engine": "faster-whisper",
        "engine_version": "1.2.1",
        "model": "small",
        "model_revision": "r",
        "device": "cpu",
        "compute_type": "int8",
        "language_requested": None,
        "language": "en",
        "language_probability": 0.97,
        "duration_s": 2.0,
        "params_hash": "h1",
        "params": {},
        "segments": normalize_segments(DEFAULT_SEGMENTS, 2.0).segments,
        "processing_s": 0.4,
    }
    values.update(changes)
    TranscriptRepository(conn).save(NewTranscript(**values))


def _run(page: str) -> AppTest:
    app = AppTest.from_file(str(APP / "views" / page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _button(app: AppTest, label: str) -> Any:
    return next(b for b in app.button if b.label == label)


# ------------------------------------------------------------------ formatting


def test_transcript_formatting() -> None:
    assert timestamp(0.9) == "0:00.900" and timestamp(3725.5) == "1:02:05.500"
    assert language_label("ur", 0.42) == "Urdu (ur, 42%)" and language_label(None) == "Unknown"
    assert escape_markdown("*a* [b]") == "\\*a\\* \\[b\\]"
    assert highlight_snippet("say \x02money\x03 *now*") == "say **money** \\*now\\*"
    assert transcript_cell(None, None) == "· not started"


def test_job_labels_for_new_kinds(db_conn: sqlite3.Connection, video: Video) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_model_download("small")
    job = queue.get(job_id)
    assert job is not None and job_target(job) == "model small"
    t_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    t_job = queue.get(t_id)
    assert t_job is not None and transcript_cell(None, t_job) == "⏳ queued"
    assert job_outcome(t_job) == ""


# ------------------------------------------------------------------ transcripts page


def test_page_without_videos_or_models(app_env: None) -> None:
    app = _run("transcripts.py")
    assert any("No videos imported yet" in i.value for i in app.info)
    table = app.dataframe[0].value
    assert table["Model"].tolist()[:3] == ["tiny", "base", "small"]
    assert set(table["Status"]) == {"· not downloaded"}
    download = _button(app, DOWNLOAD_SMALL)
    assert not download.disabled


def test_download_button_queues_a_download_job(app_env: None, db_conn: sqlite3.Connection) -> None:
    app = _run("transcripts.py")
    _button(app, DOWNLOAD_SMALL).click()
    app.run()
    assert any("Queued download job" in s.value for s in app.success)
    job = JobQueue(db_conn).active_download("small")
    assert job is not None and job.status is JobStatus.QUEUED
    app.run()
    assert any("No worker is running" in w.value for w in app.warning)
    assert any(b.label == "Cancel small download" for b in app.button)


def test_transcribe_needs_a_model(app_env: None, video: Video) -> None:
    app = _run("transcripts.py")
    assert any("No speech model is installed" in w.value for w in app.warning)
    assert not any(b.label == "Transcribe" for b in app.button)
    assert any("No transcript yet" in c.value for c in app.caption)


def test_transcribe_button_queues_a_job(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _install(settings, "small")
    app = _run("transcripts.py")
    language = next(s for s in app.selectbox if s.label == "Language")
    language.set_value("Urdu (ur)")
    _button(app, "Transcribe").click()
    app.run()
    assert not app.exception
    assert any("Queued transcription job" in s.value for s in app.success)
    job = JobQueue(db_conn).active_transcription(video.id)
    assert job is not None
    assert job.payload == {"video_id": video.id, "model": "small", "language": "ur", "force": False}
    app.run()
    assert _button(app, "Transcribe").disabled
    _button(app, "Cancel transcription").click()
    app.run()
    assert any("Cancelled before it started" in s.value for s in app.success)


def test_failed_job_is_shown_with_retry_hint(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _install(settings, "small")
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(video.id, model="small", language=None)
    assert queue.claim("w") is not None
    queue.fail(job_id, "Speech recognition failed: RuntimeError: boom", worker_id="w")
    app = _run("transcripts.py")
    assert any("boom" in e.value for e in app.error)
    assert any("press Transcribe to retry" in c.value for c in app.caption)
    assert not _button(app, "Transcribe").disabled


def test_transcript_viewer_downloads_and_search(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _save(db_conn, video.id)
    app = _run("transcripts.py")
    segments = next(d.value for d in app.dataframe if "Text" in d.value.columns)
    assert segments["Text"].tolist() == ["Hello world.", "Money talks, café!"]
    assert segments["Start"].tolist() == ["0:00.000", "0:00.900"]
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Segments"] == "2" and metrics["Language"] == "English"
    assert any("language English (en, 97%)" in c.value for c in app.caption)
    downloads = [b for b in app.get("download_button")]
    assert len(downloads) == 4

    app.text_input(key="search-query").set_value("cafe")
    app.run()
    assert any("**café**" in m.value for m in app.markdown)
    app.text_input(key="search-query").set_value("zebra")
    app.run()
    assert any("No matches" in c.value for c in app.caption)


def test_versions_empty_and_uncertain_language(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    _save(db_conn, video.id, params_hash="old")
    _save(
        db_conn,
        video.id,
        params_hash="new",
        model="tiny",
        language="cy",
        language_probability=0.3,
    )
    app = _run("transcripts.py")
    assert any("Language detection was uncertain (30%)" in w.value for w in app.warning)
    version = next(s for s in app.selectbox if s.label == "Version")
    assert str(version.value).startswith("★ tiny")
    older = next(o for o in version.options if o.startswith("small"))
    version.set_value(older)
    app.run()
    _button(app, "Use this version").click()
    app.run()
    current = TranscriptRepository(db_conn).current(video.id)
    assert current is not None and current.model == "small"

    _save(db_conn, video.id, params_hash="silent", segments=(), language=None)
    app = _run("transcripts.py")
    assert any("No speech was detected" in i.value for i in app.info)


def test_video_without_audio_cannot_be_transcribed(
    app_env: None, settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    _install(settings, "small")
    db_conn.execute("DELETE FROM media_streams WHERE codec_type = 'audio'")
    app = _run("transcripts.py")
    assert any("no audio track" in w.value for w in app.warning)
    assert not any(b.label == "Transcribe" for b in app.button)


# ------------------------------------------------------------------ library and overview


def test_library_and_overview_show_transcription_state(
    app_env: None, db_conn: sqlite3.Connection, video: Video
) -> None:
    app = _run("library.py")
    assert app.dataframe[0].value["Transcript"].tolist() == ["· not started"]
    _save(db_conn, video.id)
    app = _run("library.py")
    assert app.dataframe[0].value["Transcript"].tolist() == ["✅ done"]
    assert any("Transcript: 2 segments with small" in c.value for c in app.caption)
    overview = _run("overview.py")
    assert {m.label: m.value for m in overview.metric}["Transcribed"] == "1 / 1"
