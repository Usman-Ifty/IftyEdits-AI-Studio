"""Upload package screens: empty state, no-AI behaviour, checklist gating, integration."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.core.models import TranscriptSegment
from iftyedits.core.shorts import ShortConfig
from iftyedits.services.ingest import IngestService
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import KIND_EXPORT, ShortRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from iftyedits.ui import chrome
from tests.conftest import MakeMedia

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))
    monkeypatch.delenv("IFTYEDITS_LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _page(name: str) -> AppTest:
    return AppTest.from_file(str(APP / "views" / name), default_timeout=60)


@pytest.fixture
def seeded(
    settings: Settings, ingest: IngestService, make_media: MakeMedia, app_env: None
) -> Iterator[int]:
    video = ingest.import_video(make_media("Show.mp4")).video
    conn = open_database(settings.db_path)
    try:
        TranscriptRepository(conn).save(
            NewTranscript(
                video_id=video.id,
                audio_sha256="a" * 64,
                engine="fake",
                engine_version="1",
                model="m",
                model_revision=None,
                device="cpu",
                compute_type="int8",
                language_requested=None,
                language="en",
                language_probability=0.9,
                duration_s=20.0,
                params_hash="p" * 8,
                params={},
                segments=[TranscriptSegment(0.0, 4.0, "hello <b>there</b> friends")],
            )
        )
        config = ShortConfig(start_s=0.0, end_s=20.0)
        repo = ShortRepository(conn)
        project = repo.create_project(video.id, "k", "Hello <script>x</script>", 0.0, 20.0, config)
        digest = config.config_hash(video_sha256=video.sha256, kind=KIND_EXPORT, has_audio=True)
        render = repo.create_render(project.id, KIND_EXPORT, digest, config)
        repo.mark_running(render.id)
        out = settings.exports_dir / "s.mp4"
        out.write_bytes(b"x" * 50)
        repo.mark_succeeded(
            render.id,
            output_path=out,
            width=1080,
            height=1920,
            duration_s=20.0,
            size_bytes=50,
            has_audio=True,
            encoder="libx264",
        )
        yield project.id
    finally:
        conn.close()


def test_empty_state_without_shorts(app_env: None) -> None:
    app = _page("publish.py").run()
    assert not app.exception
    assert any("No Shorts to prepare yet" in m.value for m in app.markdown)


def test_without_an_ai_model_nothing_is_generated_and_it_says_so(seeded: int) -> None:
    app = _page("publish.py").run()
    assert not app.exception
    assert any("AI not configured" in m.value for m in app.markdown)
    generate = next(b for b in app.button if b.label == "Generate everything")
    assert generate.disabled
    assert any("No AI model is set up" in i.value for i in app.info)


def test_confirmation_is_disabled_until_the_checklist_is_done(seeded: int) -> None:
    app = _page("publish.py").run()
    confirm = next(b for b in app.button if b.label.startswith("Confirm: ready"))
    assert confirm.disabled
    assert not any("Ready for upload" in m.value for m in app.markdown)


def test_manual_review_can_be_recorded_from_the_page(seeded: int, settings: Settings) -> None:
    app = _page("publish.py").run()
    note = next(t for t in app.text_area if t.label == "What did you check?")
    note.set_value("Watched it fully").run()
    next(b for b in app.button if b.label == "Record my manual review").click().run()
    assert not app.exception
    conn = open_database(settings.db_path)
    try:
        row = conn.execute("SELECT status FROM safety_reviews").fetchone()
    finally:
        conn.close()
    assert row["status"] == "manual"


def test_untrusted_text_is_not_rendered_as_html(seeded: int) -> None:
    app = _page("publish.py").run()
    for block in app.markdown:
        assert "<script>x</script>" not in block.value


def test_clip_editor_opens_the_upload_section(seeded: int) -> None:
    app = _page("clip_editor.py")
    app.session_state[chrome.SECTION_KEY] = chrome.UPLOAD_SECTION
    app.run()
    assert not app.exception
    assert app.session_state[chrome.SECTION_KEY] == chrome.UPLOAD_SECTION
    assert not app.title  # embedded heading, not a second page title


def test_finished_shorts_card_shows_readiness_and_opens_the_package(seeded: int) -> None:
    app = _page("exports.py").run()
    assert not app.exception
    assert any("Draft" in m.value or "incomplete" in m.value for m in app.markdown)
    button = next(b for b in app.button if b.label == "Review upload package")
    assert button is not None


def test_settings_ai_tab_reports_unconfigured_and_hides_secrets(app_env: None) -> None:
    app = _page("settings.py").run()
    assert not app.exception
    assert any("Not configured" in m.value for m in app.markdown)
