"""``iftyedits ui``: when a dashboard or worker is started, and when nothing may be."""

from __future__ import annotations

import http.server
import socket
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core.models import Job, JobStatus
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.storage.db import open_database, open_read_only


class FakeChild:
    """Stands in for a Streamlit or worker process; nothing is executed."""

    def __init__(self, argv: list[str], exit_code: int | None) -> None:
        self.argv = argv
        self.kind = "worker" if argv[-1] == "worker" else "dashboard"
        self.exit_code = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            self.exit_code = 0
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class Launcher:
    """Records every process ``cmd_ui`` would start."""

    def __init__(self) -> None:
        self.children: list[FakeChild] = []
        self.events: list[str] = []
        self.dashboard_exit: int | None = None
        self.port_busy = False
        self.health: list[bool] = [True]

    def popen(self, argv: list[str], **_: Any) -> FakeChild:
        child = FakeChild(argv, None)
        if child.kind == "dashboard":
            child.exit_code = self.dashboard_exit
        self.children.append(child)
        self.events.append(f"spawn {child.kind}")
        return child

    def healthy(self, address: str, port: int, timeout_s: float = 2.0) -> bool:
        answer = self.health.pop(0) if len(self.health) > 1 else self.health[0]
        self.events.append(f"health {answer}")
        return answer

    @property
    def kinds(self) -> list[str]:
        return [c.kind for c in self.children]


@pytest.fixture(autouse=True)
def no_job_may_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to claim or run a job in this module fails the test."""

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a startup path tried to claim or run a job")

    monkeypatch.setattr(JobQueue, "claim", refuse)
    monkeypatch.setattr(Worker, "run", refuse)
    monkeypatch.setattr(Worker, "process_one", refuse)


@pytest.fixture
def ui_home(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    home = tmp_path / "home"
    (home / "app").mkdir(parents=True)
    (home / "app" / "Home.py").write_text("")
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("signal.signal", lambda *_: None)
    return settings


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> Launcher:
    fake = Launcher()
    monkeypatch.setattr("subprocess.Popen", fake.popen)
    monkeypatch.setattr(cli, "port_in_use", lambda *_: fake.port_busy)
    monkeypatch.setattr(cli, "dashboard_healthy", fake.healthy)
    monkeypatch.setattr(cli, "DASHBOARD_POLL_S", 0.0)
    return fake


def queue_job(settings: Settings) -> int:
    with closing(open_database(settings.db_path)) as conn:
        return JobQueue(conn).enqueue(PRODUCE_VIDEO, {"video_id": 1, "options": {}})


def add_live_worker(settings: Settings) -> None:
    with closing(open_database(settings.db_path)) as conn:
        JobQueue(conn).register_worker("other-worker", 4242)


def job(settings: Settings, job_id: int) -> Job:
    with closing(open_database(settings.db_path)) as conn:
        found = JobQueue(conn).get(job_id)
    assert found is not None
    return found


def assert_untouched(settings: Settings, job_id: int) -> None:
    after = job(settings, job_id)
    assert after.status is JobStatus.QUEUED
    assert after.attempts == 0
    assert after.worker_id is None and after.started_at is None


# ------------------------------------------------------------------ occupied port


@pytest.mark.parametrize("flags", [[], ["--no-worker"]])
def test_running_dashboard_without_worker_starts_nothing(
    ui_home: Settings,
    launcher: Launcher,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    job_id = queue_job(ui_home)
    launcher.port_busy = True

    assert cli.main(["ui", *flags]) == cli.EXIT_OK

    assert launcher.children == []
    assert_untouched(ui_home, job_id)
    out = capsys.readouterr().out
    assert "already running" in out and "No worker is active" in out
    assert "1 queued job(s) will wait. Nothing was started." in out
    assert ("--start-worker" in out) is (flags == [])


def test_running_dashboard_with_worker_starts_nothing(
    ui_home: Settings, launcher: Launcher, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = queue_job(ui_home)
    add_live_worker(ui_home)
    launcher.port_busy = True

    for flags in ([], ["--start-worker"], ["--no-worker"]):
        assert cli.main(["ui", *flags]) == cli.EXIT_OK

    assert launcher.children == []
    assert_untouched(ui_home, job_id)
    assert "Dashboard and worker already running" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [[], ["--no-worker"], ["--start-worker"]])
def test_port_taken_by_something_else_starts_nothing(
    ui_home: Settings,
    launcher: Launcher,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    job_id = queue_job(ui_home)
    launcher.port_busy = True
    launcher.health = [False]

    assert cli.main(["ui", *flags]) == cli.EXIT_FAILED

    assert launcher.children == []
    assert_untouched(ui_home, job_id)
    assert "Nothing was started" in capsys.readouterr().err


def test_start_worker_opt_in_starts_one_worker_and_lists_jobs(
    ui_home: Settings, launcher: Launcher, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = queue_job(ui_home)
    launcher.port_busy = True

    assert cli.main(["ui", "--start-worker"]) == cli.EXIT_OK

    assert launcher.kinds == ["worker"]
    assert f"#{job_id} produce_video" in capsys.readouterr().out
    assert_untouched(ui_home, job_id)  # the fake worker never runs anything


@pytest.mark.parametrize(("flags", "code"), [([], 0), (["--start-worker"], 1)])
def test_unreadable_database_starts_nothing(
    ui_home: Settings, launcher: Launcher, flags: list[str], code: int
) -> None:
    ui_home.db_path.parent.mkdir(parents=True, exist_ok=True)
    ui_home.db_path.write_bytes(b"this is not a database" * 100)
    launcher.port_busy = True

    assert cli.main(["ui", *flags]) == code

    assert launcher.children == []


# ------------------------------------------------------------------ free port


def test_worker_starts_only_after_the_dashboard_is_healthy(
    ui_home: Settings, launcher: Launcher
) -> None:
    launcher.health = [False, False, True]

    assert cli.main(["ui"]) == cli.EXIT_OK

    assert launcher.events == [
        "spawn dashboard",
        "health False",
        "health False",
        "health True",
        "spawn worker",
    ]


@pytest.mark.parametrize("exit_code", [1, 0])
def test_dashboard_exiting_early_never_starts_a_worker(
    ui_home: Settings, launcher: Launcher, exit_code: int
) -> None:
    job_id = queue_job(ui_home)
    launcher.dashboard_exit = exit_code

    assert cli.main(["ui"]) == cli.EXIT_FAILED

    assert launcher.kinds == ["dashboard"]
    assert_untouched(ui_home, job_id)


def test_dashboard_never_healthy_never_starts_a_worker(
    ui_home: Settings, launcher: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = queue_job(ui_home)
    launcher.health = [False]
    monkeypatch.setattr(cli, "DASHBOARD_START_TIMEOUT_S", 0.0)

    assert cli.main(["ui"]) == cli.EXIT_FAILED

    assert launcher.kinds == ["dashboard"]
    assert launcher.children[0].terminated is True
    assert_untouched(ui_home, job_id)


def test_no_worker_flag_starts_only_the_dashboard(ui_home: Settings, launcher: Launcher) -> None:
    job_id = queue_job(ui_home)

    assert cli.main(["ui", "--no-worker"]) == cli.EXIT_OK

    assert launcher.kinds == ["dashboard"]
    assert_untouched(ui_home, job_id)


def test_free_port_does_not_start_a_second_worker(
    ui_home: Settings, launcher: Launcher, capsys: pytest.CaptureFixture[str]
) -> None:
    add_live_worker(ui_home)

    assert cli.main(["ui"]) == cli.EXIT_OK

    assert launcher.kinds == ["dashboard"]
    assert "already active" in capsys.readouterr().out


def test_start_worker_and_no_worker_cannot_be_combined() -> None:
    assert cli.build_parser().parse_args(["ui"]).start_worker is False
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ui", "--no-worker", "--start-worker"])


def test_read_only_database_cannot_be_written(settings: Settings) -> None:
    queue_job(settings)
    with closing(open_read_only(settings.db_path)) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM jobs")


# ------------------------------------------------------------------ the real probes


@pytest.fixture
def serve() -> Iterator[Callable[[int, bytes], int]]:
    servers: list[http.server.HTTPServer] = []

    def start(status: int, body: bytes) -> int:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_port

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("status", "body", "healthy"),
    [(200, b"ok", True), (200, b"<html>", False), (404, b"ok", False)],
)
def test_probes_against_a_real_server(
    serve: Callable[[int, bytes], int], status: int, body: bytes, healthy: bool
) -> None:
    port = serve(status, body)
    assert cli.port_in_use("127.0.0.1", port) is True
    assert cli.dashboard_healthy("127.0.0.1", port) is healthy


def test_probes_on_a_free_port() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert cli.port_in_use("127.0.0.1", port) is False
    assert cli.dashboard_healthy("127.0.0.1", port, timeout_s=0.5) is False


def test_real_healthy_server_on_the_port_starts_nothing(
    ui_home: Settings,
    serve: Callable[[int, bytes], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = queue_job(ui_home)
    port = serve(200, b"ok")

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr("subprocess.Popen", refuse)

    assert cli.main(["ui", "--port", str(port)]) == cli.EXIT_OK

    assert_untouched(ui_home, job_id)
