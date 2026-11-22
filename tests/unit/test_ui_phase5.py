"""Phase 5: eight-section navigation, grouped pages, journey tracker and gallery states."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.config import Settings
from iftyedits.services.ingest import IngestService
from iftyedits.services.journey import STEP_COUNT, latest_progress
from iftyedits.storage.db import open_database
from iftyedits.ui import chrome, theme
from tests.conftest import MakeMedia

APP = Path(__file__).resolve().parents[2] / "app"
SECTIONS = [
    "Dashboard",
    "Create Shorts",
    "My Videos",
    "Clip Editor",
    "Finished Shorts",
    "Processing Queue",
    "Settings",
    "Guides & Help",
]
WRAPPERS = {
    "my_videos.py": {"library.py", "transcripts.py"},
    "clip_editor.py": {"clips.py", "editor.py", "publish.py"},
}


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))


def _page(name: str) -> AppTest:
    return AppTest.from_file(str(APP / "views" / name), default_timeout=60)


def _pages() -> list[tuple[str, str]]:
    tree = ast.parse((APP / "Home.py").read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Page":
            title = next(k.value for k in node.keywords if k.arg == "title")
            assert isinstance(node.args[0], ast.Constant) and isinstance(title, ast.Constant)
            found.append((str(title.value), str(node.args[0].value)))
    return found


def test_navigation_has_the_eight_sections_in_order() -> None:
    assert [t for t, _ in _pages()] == SECTIONS


def test_every_navigation_target_exists() -> None:
    for _, path in _pages():
        assert (APP / path).is_file(), path


def test_no_view_is_orphaned_from_navigation() -> None:
    registered = {Path(p).name for _, p in _pages()}
    reachable = set(registered)
    for wrapper, children in WRAPPERS.items():
        assert wrapper in registered
        text = (APP / "views" / wrapper).read_text(encoding="utf-8")
        for child in children:
            assert f'"{child[:-3]}"' in text, f"{wrapper} does not run {child}"
            reachable.add(child)
    assert {p.name for p in (APP / "views").glob("*.py")} <= reachable


def test_home_runs(app_env: None) -> None:
    app = AppTest.from_file(str(APP / "Home.py"), default_timeout=60).run()
    assert not app.exception


@pytest.mark.parametrize("page", ["my_videos.py", "clip_editor.py", "settings.py"])
def test_new_pages_run(page: str, app_env: None) -> None:
    assert not _page(page).run().exception


def test_an_empty_library_does_not_hide_the_transcripts_panel(app_env: None) -> None:
    app = _page("my_videos.py").run()
    assert any("No videos yet" in i.value for i in app.info)
    assert len(app.tabs) == 2
    assert any("No videos imported yet" in i.value for i in app.info)
    assert any(
        "Transcripts" in str(m.value) or "transcript" in str(m.value).lower()
        for m in [*app.subheader, *app.info, *app.caption, *app.markdown]
    )


def test_home_resets_the_embed_flag(app_env: None) -> None:
    app = AppTest.from_file(str(APP / "Home.py"), default_timeout=60)
    app.session_state[chrome.EMBED_KEY] = True
    app.run()
    assert app.session_state[chrome.EMBED_KEY] is False


def test_clip_editor_opens_the_editor_section_on_request(app_env: None) -> None:
    app = _page("clip_editor.py")
    app.session_state[chrome.SECTION_KEY] = chrome.EDITOR_SECTION
    app.run()
    assert not app.exception
    assert app.session_state[chrome.SECTION_KEY] == chrome.EDITOR_SECTION


def test_embedded_views_use_section_headings(app_env: None) -> None:
    assert not _page("my_videos.py").run().title


def test_journey_marks_done_then_next() -> None:
    html = theme.journey((True, True, False))
    assert html.count("ie-step done") == 2
    assert html.count("ie-step next") == 1
    assert theme.journey((False, True)).count("ie-step done") == 0
    assert len(theme.JOURNEY) == STEP_COUNT == 7


def test_helpers_escape_text() -> None:
    assert "<script>" not in theme.empty_state("<script>", "x")
    assert "<script>" not in theme.gallery_card("<script>", "m")


def test_progress_empty_then_after_import(
    settings: Settings, ingest: IngestService, make_media: MakeMedia
) -> None:
    conn = open_database(settings.db_path)
    try:
        assert latest_progress(conn) == ((False,) * STEP_COUNT, "")
        ingest.import_video(make_media("ep.mp4"))
        flags, name = latest_progress(conn)
        assert flags[:2] == (True, True) and not any(flags[2:]) and name == "ep.mp4"
    finally:
        conn.close()


def test_finished_shorts_empty_state(app_env: None) -> None:
    app = _page("exports.py").run()
    assert not app.exception
    assert any("No Shorts yet" in m.value for m in app.markdown)


def test_queue_page_shows_status_counts(app_env: None) -> None:
    app = _page("jobs.py").run()
    assert not app.exception
    labels = {m.label for m in app.metric}
    assert labels >= {"Running", "Queued", "Completed", "Failed", "Cancelled"}
