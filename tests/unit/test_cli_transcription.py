"""CLI: models, transcribe, transcript and search (no network, no speech engine)."""

from __future__ import annotations

import io
from contextlib import closing
from pathlib import Path

import pytest

from iftyedits import cli
from iftyedits.adapters.model_store import ModelStore
from iftyedits.config import Settings
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.storage.db import open_database
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.conftest import MakeMedia
from tests.transcription_fakes import DEFAULT_SEGMENTS, TEST_SPEC, FakeModelStore
from tests.unit.test_model_store import CONFIG, SPEC, WEIGHTS


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


def _fake_store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(cli, "build_model_store", lambda _settings: store)


def _save_transcript(settings: Settings, video_id: int) -> None:
    with closing(open_database(settings.db_path)) as conn:
        TranscriptRepository(conn).save(
            NewTranscript(
                video_id=video_id,
                audio_sha256="c" * 64,
                engine="fake-engine",
                engine_version="1",
                model="small",
                model_revision=TEST_SPEC.revision,
                device="cpu",
                compute_type="int8",
                language_requested=None,
                language="en",
                language_probability=0.9,
                duration_s=2.0,
                params_hash="h",
                params={},
                segments=DEFAULT_SEGMENTS,
                processing_s=0.5,
            )
        )


def test_models_list_and_download(
    cli_env: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"config.json": CONFIG, "model.bin": WEIGHTS}
    store = ModelStore(
        cli_env.models_dir,
        catalog=(SPEC,),
        opener=lambda url, timeout: io.BytesIO(files[url.rsplit("/", 1)[1]]),
    )
    _fake_store(monkeypatch, store)
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "mini" in out and "not downloaded" in out
    assert cli.main(["models", "download"]) == cli.EXIT_USAGE
    assert cli.main(["models", "verify", "mini"]) == cli.EXIT_FAILED
    capsys.readouterr()
    assert cli.main(["models", "download", "mini"]) == 0
    captured = capsys.readouterr()
    assert "installed and verified" in captured.out and "100%" in captured.err
    assert cli.main(["models", "download", "mini"]) == 0
    assert "already installed" in capsys.readouterr().out
    assert cli.main(["models", "verify", "mini"]) == 0
    assert cli.main(["models", "download", "giant"]) == cli.EXIT_FAILED
    assert "Unknown speech model" in capsys.readouterr().err


def test_transcribe_requires_installed_model_then_queues(
    cli_env: Settings,
    video: Video,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeModelStore(cli_env.models_dir, installed=())
    _fake_store(monkeypatch, store)
    assert cli.main(["transcribe", str(video.id)]) == cli.EXIT_FAILED
    assert "models download small" in capsys.readouterr().err
    assert cli.main(["transcribe"]) == cli.EXIT_USAGE

    store.installed.add("small")
    assert cli.main(["transcribe", str(video.id), "--language", "en"]) == 0
    assert "queued" in capsys.readouterr().out
    assert cli.main(["transcribe", "--all"]) == 0
    assert "already queued" in capsys.readouterr().out
    with closing(open_database(cli_env.db_path)) as conn:
        job = JobQueue(conn).active_transcription(video.id)
    assert job is not None
    assert job.payload == {"video_id": video.id, "model": "small", "language": "en", "force": False}
    assert cli.main(["transcribe", str(video.id), "--language", "xx"]) == cli.EXIT_FAILED
    assert "Unsupported language" in capsys.readouterr().err


def test_transcript_print_save_and_search(
    cli_env: Settings, video: Video, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["transcript", str(video.id)]) == cli.EXIT_FAILED
    assert "no transcript yet" in capsys.readouterr().err
    _save_transcript(cli_env, video.id)

    assert cli.main(["transcript", str(video.id)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Hello world.\nMoney talks, café!\n"
    assert "# Language: English (en, 90%)" in captured.err

    assert cli.main(["transcript", str(video.id), "--format", "srt"]) == 0
    assert capsys.readouterr().out.startswith("1\n00:00:00,000 --> 00:00:00,800\nHello world.\n")

    assert cli.main(["transcript", str(video.id), "--format", "vtt", "--save"]) == 0
    saved = Path(capsys.readouterr().out.removeprefix("Saved ").strip())
    assert saved.is_relative_to(cli_env.exports_dir.resolve())
    assert saved.name == "My Podcast-small.vtt"
    assert saved.read_text(encoding="utf-8").startswith("WEBVTT\n")

    assert cli.main(["search", "money"]) == 0
    out = capsys.readouterr().out
    assert "#1 My Podcast.mp4" in out and "0:00.900" in out and "Money talks" in out
    assert cli.main(["search", "nothing-here", "--video", str(video.id)]) == 0
    assert "No matches." in capsys.readouterr().out


def test_check_reports_engine_and_models(
    cli_env: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_store(monkeypatch, FakeModelStore(cli_env.models_dir, installed=()))
    monkeypatch.setattr(cli, "engine_version", lambda: "9.9")
    cli.main(["check"])
    out = capsys.readouterr().out
    assert "faster-whisper 9.9" in out
    assert "installed: none" in out and "default model small is not downloaded" in out
    monkeypatch.setattr(cli, "engine_version", lambda: None)
    assert cli.main(["check"]) == cli.EXIT_FAILED
    assert "NOT INSTALLED" in capsys.readouterr().out


def test_transcription_commands_have_help(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("models", "transcribe", "transcript", "search"):
        with pytest.raises(SystemExit) as exc:
            cli.main([command, "--help"])
        assert exc.value.code == 0
    assert "--language" in capsys.readouterr().out
