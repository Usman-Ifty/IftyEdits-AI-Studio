"""Phase 6.10: Phase 6 workflows through the provider router, and the Settings AI tab."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.adapters.ai_router import AiConfig, RoutedModel
from iftyedits.config import Settings
from iftyedits.core import usage as us
from iftyedits.core.errors import LlmUnavailableError
from iftyedits.core.models import TranscriptSegment, Video
from iftyedits.core.shorts import ShortConfig
from iftyedits.services.ingest import IngestService
from iftyedits.services.publishing import PublishingService
from iftyedits.services.upload_package import build_package
from iftyedits.storage.ai import AiRepository
from iftyedits.storage.shorts import ShortProject, ShortRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.conftest import MakeMedia

APP = Path(__file__).resolve().parents[2] / "app"
META = {
    "titles": [{"text": "The park meeting", "style": "informative"}],
    "recommended_index": 0,
    "description": "A short scene at the park.",
    "hashtags": ["#park", "#shorts"],
    "tags": ["park", "meeting"],
    "summary": "Two people meet.",
    "confidence": "medium",
}
SAFE = {"flags": [], "summary": "Nothing concerning.", "confidence": "high"}


class Scripted:
    def __init__(
        self, name: str, script: list[str | Exception], usage: us.Usage | None = None
    ) -> None:
        self._name, self.script, self._usage = name, list(script), usage
        self.last_usage: us.Usage | None = None
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        self.prompts.append(user)
        self.last_usage = self._usage
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def cfg(mode: str) -> AiConfig:
    return AiConfig(mode, us.Limits(0.0, 0, False), us.Prices(None, None), 2048)


@pytest.fixture
def project(
    db_conn: sqlite3.Connection, ingest: IngestService, make_media: MakeMedia
) -> ShortProject:
    video: Video = ingest.import_video(make_media("Talk.mp4")).video
    segs = [
        TranscriptSegment(i * 5.0, i * 5.0 + 4.0, t)
        for i, t in enumerate(["we met at the park", "then we left"])
    ]
    TranscriptRepository(db_conn).save(
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
            language_probability=0.99,
            duration_s=20.0,
            params_hash="p" * 8,
            params={},
            segments=segs,
        )
    )
    return ShortRepository(db_conn).create_project(
        video.id, "k", "Park scene", 0.0, 20.0, ShortConfig(start_s=0.0, end_s=20.0)
    )


def service(
    settings: Settings,
    conn: sqlite3.Connection,
    mode: str,
    claude: Scripted | None,
    local: Scripted | None,
) -> PublishingService:
    model = RoutedModel(cfg(mode), claude, None if claude else "no key", local, AiRepository(conn))
    return PublishingService(settings, conn, model)


def test_metadata_records_claude_provider_and_usage(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    c = Scripted("claude-test", [json.dumps(META)], us.Usage(300, 120))
    svc = service(settings, db_conn, "claude", c, None)
    assert svc.generate_metadata(project.id).ok
    stored = svc.metadata(project.id)
    assert stored is not None and stored.model == "claude:claude-test"
    month = datetime.now(UTC).strftime("%Y-%m")
    totals = AiRepository(db_conn).totals("claude", month)
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (1, 300, 120)
    features = [f for f, _ in AiRepository(db_conn).by_feature("claude", month)]
    assert len(features) == 1 and features[0].startswith("metadata:")


def test_hybrid_fallback_is_visible_in_result_and_stored_model(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    c = Scripted("claude-test", [LlmUnavailableError("down")])
    o = Scripted("llama3", [json.dumps(META)])
    svc = service(settings, db_conn, "hybrid", c, o)
    result = svc.generate_metadata(project.id)
    assert result.ok and any("Claude could not answer" in n for n in result.notes)
    stored = svc.metadata(project.id)
    assert stored is not None and stored.model == "ollama:llama3"


def test_claude_mode_failure_changes_nothing_and_keeps_manual_fields(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    svc = service(
        settings,
        db_conn,
        "claude",
        Scripted("c", [LlmUnavailableError("down")]),
        Scripted("l", [json.dumps(META)]),
    )
    svc.edit_metadata(project.id, description="My own words.")
    result = svc.generate_metadata(project.id, ["titles"])
    assert not result.ok
    stored = svc.metadata(project.id)
    assert stored is not None and stored.metadata.description == "My own words."
    assert stored.provenance["description"] == "manual"


def test_regenerating_one_group_keeps_other_manual_fields(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    c = Scripted("c", [json.dumps(META)] * 2, us.Usage(1, 1))
    svc = service(settings, db_conn, "claude", c, None)
    svc.edit_metadata(project.id, description="Mine.", selected_title="My title")
    assert svc.generate_metadata(project.id, ["hashtags"]).ok
    stored = svc.metadata(project.id)
    assert stored is not None
    assert stored.metadata.description == "Mine." and stored.metadata.selected_title == "My title"
    assert stored.provenance["hashtags"] == "ai" and stored.provenance["description"] == "manual"


def test_safety_review_runs_through_router_and_shows_provider(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    c = Scripted("claude-test", [json.dumps(SAFE)], us.Usage(50, 20))
    svc = service(settings, db_conn, "claude", c, None)
    review = svc.run_review(project.id)
    assert review.model == "claude:claude-test"
    assert [r.feature for r in AiRepository(db_conn).recent(3)] == ["safety_review"]


def test_failed_review_never_counts_as_passed(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    svc = service(settings, db_conn, "claude", Scripted("c", [LlmUnavailableError("down")]), None)
    svc.run_review(project.id)
    assert svc.facts(project.id).review_status not in ("completed", "manual")


def test_package_reports_provider_labels(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    svc = service(settings, db_conn, "claude", Scripted("c1", [json.dumps(META)]), None)
    svc.generate_metadata(project.id)
    package = build_package(svc, project.id)
    assert package["generated_with"]["metadata_model"] == "claude:c1"
    assert "sk-" not in json.dumps(package)


# ------------------------------------------------------------------ Settings page


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))
    monkeypatch.delenv("IFTYEDITS_LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def page() -> AppTest:
    return AppTest.from_file(str(APP / "views" / "settings.py"), default_timeout=60)


def test_settings_ai_tab_without_key_explains_setup(
    app_env: None, db_conn: sqlite3.Connection
) -> None:
    at = page().run()
    assert not at.exception
    text = " ".join(str(c.value) for c in at.caption)
    assert (
        "ANTHROPIC_API_KEY" in " ".join(str(c.value) for c in at.caption)
        or "ANTHROPIC_API_KEY" in text
    )
    assert any(r.label == "AI provider" for r in at.radio)


def test_settings_save_persists_mode_and_never_shows_key(
    app_env: None, db_conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-" + "ant-SHOULD-NOT-APPEAR"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    at = page().run()
    next(r for r in at.radio if r.label == "AI provider").set_value("hybrid")
    next(b for b in at.button if b.label == "Save AI settings").click()
    at.run()
    assert not at.exception
    assert AiRepository(db_conn).get_setting("mode") == "hybrid"
    rendered = " ".join(
        str(e.value) for e in [*at.markdown, *at.caption, *at.info, *at.warning, *at.success]
    )
    assert secret not in rendered
    assert secret not in "\n".join(db_conn.iterdump())
