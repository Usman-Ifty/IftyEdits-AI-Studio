"""Regression: pages load, their imports exist, and the CLI and UI share one database."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core import errors
from iftyedits.pipeline import jobs
from iftyedits.services.ingest import IngestService
from tests.conftest import MakeMedia

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
PAGES = sorted((APP / "views").glob("*.py"))


@pytest.fixture
def app_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))


def test_the_page_list_is_not_empty() -> None:
    assert {p.name for p in PAGES} >= {"clips.py", "library.py", "overview.py", "jobs.py"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_page_runs_without_exception(page: Path, app_env: None) -> None:
    app = AppTest.from_file(str(page), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]


@pytest.mark.parametrize("source", [APP / "Home.py", *PAGES], ids=lambda p: p.name)
def test_names_imported_by_pages_exist(source: Path) -> None:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("iftyedits"):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name) or _is_submodule(node.module, alias.name), (
                    f"{source.name}: cannot import {alias.name} from {node.module}"
                )


def _is_submodule(package: str, name: str) -> bool:
    try:
        importlib.import_module(f"{package}.{name}")
    except ImportError:
        return False
    return True


def test_job_kinds_are_defined_and_distinct() -> None:
    kinds = [
        jobs.IMPORT_FILE,
        jobs.PROCESS_VIDEO,
        jobs.TRANSCRIBE_VIDEO,
        jobs.DOWNLOAD_MODEL,
        jobs.ANALYZE_CLIPS,
    ]
    assert len(set(kinds)) == len(kinds)
    assert jobs.ANALYZE_CLIPS == "analyze_clips"


def test_clip_errors_are_defined_in_core_errors() -> None:
    assert issubclass(errors.ClipAnalysisError, errors.IftyEditsError)
    assert issubclass(errors.ClipNotFoundError, errors.ClipAnalysisError)


def test_cli_and_pages_resolve_the_same_database(
    app_env: None,
    settings: Settings,
    ingest: IngestService,
    make_media: MakeMedia,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ingest.import_video(make_media("Shared.mp4"))
    assert Settings.from_env().db_path == settings.db_path
    assert cli.main(["list"]) == 0
    assert "Shared.mp4" in capsys.readouterr().out
    app = AppTest.from_file(str(APP / "views" / "library.py"), default_timeout=60)
    app.run()
    assert not app.exception
    assert app.selectbox[0].options == ["#1  Shared.mp4"]
    assert cli.main(["show", "1"]) == 0
    assert str(settings.db_path.parent) not in capsys.readouterr().err


def test_a_video_imported_later_is_visible_to_a_running_cli(
    app_env: None, ingest: IngestService, make_media: MakeMedia, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 'No video with id 1' report: the lookup ran before the import finished."""
    assert cli.main(["show", "1"]) != 0
    capsys.readouterr()
    ingest.import_video(make_media("Late.mp4"))
    assert cli.main(["show", "1"]) == 0
    assert "Late.mp4" in capsys.readouterr().out
