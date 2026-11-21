"""CLI commands that do not need ffprobe."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import IngestService
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


def test_show_by_id_and_prefix_with_verify(
    cli_env: Settings,
    ingest: IngestService,
    make_media: MakeMedia,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_media("Café 東京 interview.mp4")
    video = ingest.import_video(source).video
    ingest.import_video(make_media("copy.MOV", kind=b"", body=source.read_bytes()))

    assert cli.main(["show", str(video.id), "--verify"]) == 0
    out = capsys.readouterr().out
    assert "Café 東京 interview.mp4" in out
    assert "integrity   OK" in out
    assert "sources     2  (duplicate imports)" in out

    assert cli.main(["show", video.sha256[:8]]) == 0
    assert video.sha256 in capsys.readouterr().out


@pytest.mark.parametrize("ref", ["999", "abc", "zzzzzzzz", "ffffffff"])
def test_show_unknown_reference_fails(
    cli_env: Settings, ref: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["show", ref]) == 1
    assert "Error:" in capsys.readouterr().err


def test_show_verify_detects_tampering(
    cli_env: Settings,
    ingest: IngestService,
    make_media: MakeMedia,
    capsys: pytest.CaptureFixture[str],
) -> None:
    video = ingest.import_video(make_media("a.mp4")).video
    stored = cli_env.originals_dir / video.vault_relpath
    stored.chmod(0o644)
    stored.write_bytes(b"tampered")
    assert cli.main(["show", str(video.id), "--verify"]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_import_folder_summary_counts_skipped(
    cli_env: Settings, make_media: MakeMedia, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = make_media("notes.txt").parent
    make_media(".hidden.mp4")
    assert cli.main(["enqueue", str(folder)]) == 1
    err = capsys.readouterr().err
    assert "notes.txt" in err and "unsupported file extension" in err
    assert ".hidden.mp4" in err and "hidden file" in err


# ------------------------------------------------------------------ Phase 1B commands


def test_enqueue_with_process_flag(
    cli_env: Settings, make_media: MakeMedia, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = make_media("a.mp4")
    assert cli.main(["enqueue", "--process", str(clip)]) == 0
    conn = open_database(cli_env.db_path)
    try:
        (job,) = JobQueue(conn).list_recent()
    finally:
        conn.close()
    assert job.payload == {"path": str(clip), "process": True}


def test_process_jobs_cancel_and_artifacts(
    cli_env: Settings,
    ingest: IngestService,
    make_media: MakeMedia,
    capsys: pytest.CaptureFixture[str],
) -> None:
    video = ingest.import_video(make_media("talk.mp4")).video

    assert cli.main(["process"]) == 2
    assert "--all" in capsys.readouterr().err

    assert cli.main(["process", str(video.id), "--stage", "proxy", "--force"]) == 0
    assert "queued" in capsys.readouterr().out
    assert cli.main(["process", "--all"]) == 0
    assert "already queued" in capsys.readouterr().out

    conn = open_database(cli_env.db_path)
    try:
        (job,) = JobQueue(conn).list_active()
    finally:
        conn.close()
    assert job.payload == {"video_id": video.id, "stages": ["proxy"], "force": True}

    assert cli.main(["jobs", "--active"]) == 0
    out = capsys.readouterr().out
    assert f"video #{video.id}" in out and "queued" in out

    assert cli.main(["cancel", str(job.id)]) == 0
    assert f"job {job.id}: cancelled" in capsys.readouterr().out
    assert cli.main(["cancel", str(job.id), "999"]) == 1
    out = capsys.readouterr().out
    assert "already finished" in out and "job 999: no such job" in out

    assert cli.main(["jobs", "--active"]) == 0
    assert "No active jobs." in capsys.readouterr().out

    assert cli.main(["artifacts", str(video.id), "--verify"]) == 0
    out = capsys.readouterr().out
    assert "none yet" in out and "artifacts\n  none" in out


def test_artifacts_verify_detects_damage(
    cli_env: Settings,
    ingest: IngestService,
    make_media: MakeMedia,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from iftyedits.core.models import ArtifactKind, Stage
    from iftyedits.storage.originals import sha256_file
    from iftyedits.storage.processing import ProcessingRepository

    video = ingest.import_video(make_media("talk.mp4")).video
    wav = cli_env.derived_dir / "x" / "audio.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF....WAVE")
    conn = open_database(cli_env.db_path)
    try:
        repo = ProcessingRepository(conn)
        stage = repo.start_stage(video.id, Stage.AUDIO, params_hash="p", params={})
        repo.register_artifact(
            video_id=video.id,
            stage_id=stage.id,
            kind=ArtifactKind.AUDIO_WAV,
            relpath="x/audio.wav",
            size_bytes=wav.stat().st_size,
            sha256=sha256_file(wav),
            params_hash="p",
            metadata={},
        )
    finally:
        conn.close()
    assert cli.main(["artifacts", str(video.id), "--verify"]) == 0
    assert "audio_wav" in capsys.readouterr().out
    wav.write_bytes(b"changed")
    assert cli.main(["artifacts", str(video.id), "--verify"]) == 1
    assert "MISMATCH OR MISSING" in capsys.readouterr().out


def test_new_commands_have_help() -> None:
    parser = cli.build_parser()
    for command in ("process", "jobs", "cancel", "artifacts"):
        with pytest.raises(SystemExit) as info:
            parser.parse_args([command, "--help"])
        assert info.value.code == 0
    args = parser.parse_args(["process", "1", "--stage", "audio", "--stage", "thumbnail"])
    assert args.stage == ["audio", "thumbnail"]
    with pytest.raises(SystemExit):
        parser.parse_args(["process", "1", "--stage", "subtitles"])
