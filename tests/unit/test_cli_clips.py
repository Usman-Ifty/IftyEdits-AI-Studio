"""CLI: analyze and clips (list, show, favorite, reject, clear), without running a worker."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core.clip_models import ClipDecision
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from tests.clip_db_fakes import save_transcript
from tests.clip_fakes import new_analysis, podcast
from tests.conftest import MakeMedia


@pytest.fixture
def cli_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))
    return settings


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("My Podcast.mp4")).video


@pytest.fixture
def analysed(cli_env: Settings, video: Video) -> Video:
    with closing(open_database(cli_env.db_path)) as conn:
        transcript_id = save_transcript(conn, video, podcast())
        ClipRepository(conn).save(new_analysis(video.sha256, video.id, transcript_id))
    return video


def test_analyze_needs_a_transcript_then_queues(
    cli_env: Settings, video: Video, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["analyze"]) == cli.EXIT_USAGE
    assert cli.main(["analyze", str(video.id)]) == 0
    assert "no transcript yet" in capsys.readouterr().out
    with closing(open_database(cli_env.db_path)) as conn:
        assert JobQueue(conn).active_clip_analysis(video.id) is None
        save_transcript(conn, video, podcast())
    assert cli.main(["analyze", str(video.id), "--force"]) == 0
    assert "queued" in capsys.readouterr().out
    assert cli.main(["analyze", "--all"]) == 0
    assert "already queued" in capsys.readouterr().out
    with closing(open_database(cli_env.db_path)) as conn:
        job = JobQueue(conn).active_clip_analysis(video.id)
    assert job is not None and job.payload == {"video_id": video.id, "force": True}


def test_clips_without_analysis_explains(
    cli_env: Settings, video: Video, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["clips", "list", str(video.id)]) == cli.EXIT_FAILED
    assert "iftyedits analyze" in capsys.readouterr().err


def test_list_sort_and_filter(analysed: Video, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["clips", "list", str(analysed.id)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) >= 2 and lines[0].startswith("#1 ")
    assert cli.main(["clips", "list", str(analysed.id), "--sort", "start", "--min-score", "1"]) == 0
    assert cli.main(["clips", "list", str(analysed.id), "--min-score", "100"]) == 0
    assert "No suggestions match." in capsys.readouterr().out
    assert cli.main(["clips", "list", str(analysed.id), "--status", "favorites"]) == 0
    assert "No suggestions match." in capsys.readouterr().out


def test_show_explains_the_score(analysed: Video, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["clips", "show", str(analysed.id)]) == cli.EXIT_USAGE
    capsys.readouterr()
    assert cli.main(["clips", "show", str(analysed.id), "1"]) == 0
    out = capsys.readouterr().out
    for expected in ("algorithmic", "Hook", "Signals", "Excerpt", "weight"):
        assert expected in out
    assert cli.main(["clips", "show", str(analysed.id), "99"]) == cli.EXIT_FAILED
    assert "No unique suggestion" in capsys.readouterr().err


def test_favorite_reject_clear_persist(
    cli_env: Settings, analysed: Video, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["clips", "favorite", str(analysed.id)]) == cli.EXIT_USAGE
    capsys.readouterr()
    assert cli.main(["clips", "favorite", str(analysed.id), "1"]) == 0
    assert "favorite" in capsys.readouterr().out
    assert cli.main(["clips", "reject", str(analysed.id), "2"]) == 0
    capsys.readouterr()
    with closing(open_database(cli_env.db_path)) as conn:
        repo = ClipRepository(conn)
        analysis = repo.current(analysed.id)
        assert analysis is not None
        first, second = repo.candidates(analysis.id)[:2]
        assert first.decision is ClipDecision.FAVORITE and second.decision is ClipDecision.REJECTED
        key = first.key
    assert cli.main(["clips", "list", str(analysed.id), "--status", "not rejected"]) == 0
    assert "[★ favorite]" in capsys.readouterr().out
    assert cli.main(["clips", "clear", str(analysed.id), key[:6]]) == 0
    assert "no decision" in capsys.readouterr().out
    assert cli.main(["clips", "clear", str(analysed.id), "abc"]) == cli.EXIT_FAILED  # too short


def test_check_mentions_clip_scoring(cli_env: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["check"])
    assert "algorithmic scoring" in capsys.readouterr().out
