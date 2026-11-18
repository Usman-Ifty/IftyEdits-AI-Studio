"""CLI commands that do not need ffprobe."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.storage.db import open_database
from tests.conftest import MakeMedia


@pytest.fixture
def cli_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    return settings


def test_ui_defaults_to_localhost() -> None:
    args = cli.build_parser().parse_args(["ui"])
    assert args.address == "127.0.0.1"
    assert args.port == 8501


def test_enqueue_folder_then_list(
    cli_env: Settings, make_media: MakeMedia, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = make_media("a.mp4").parent
    make_media("sub/b.mp4")
    make_media("notes.txt")

    assert cli.main(["enqueue", str(folder), "--recursive"]) == 0
    assert "Queued 2 job(s)" in capsys.readouterr().out
    assert cli.main(["enqueue", str(folder), "--recursive"]) == 0
    assert "Queued 0 job(s); 2 already queued" in capsys.readouterr().out

    conn = open_database(cli_env.db_path)
    try:
        jobs = JobQueue(conn).list_recent()
    finally:
        conn.close()
    assert len(jobs) == 2
    assert {j.status for j in jobs} == {JobStatus.QUEUED}

    assert cli.main(["list"]) == 0
    assert "No videos imported yet." in capsys.readouterr().out


def test_enqueue_rejects_data_directory_files(
    cli_env: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = cli_env.derived_dir / "proxy.mp4"
    inside.write_bytes(b"FAKEVIDEO")
    assert cli.main(["enqueue", str(inside)]) == 1
    assert "application data directory" in capsys.readouterr().err


def test_invalid_configuration_exit_code(
    cli_env: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IFTYEDITS_LOG_LEVEL", "LOUD")
    assert cli.main(["list"]) == 2
    assert "Invalid configuration" in capsys.readouterr().err


def test_env_file_is_read_from_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("IFTYEDITS_DATA_DIR=custom-data\n")
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.delenv("IFTYEDITS_DATA_DIR", raising=False)
    assert Settings.from_env().data_dir == (home / "custom-data").resolve()
