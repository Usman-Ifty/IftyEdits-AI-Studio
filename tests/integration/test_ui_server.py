"""Start the real Streamlit server through the CLI and check it binds to localhost only."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listening_addresses(port: int) -> list[str]:
    lsof = shutil.which("lsof")
    if lsof is None:
        return []
    out = subprocess.run(
        [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [line.split()[8] for line in out.splitlines()[1:] if len(line.split()) > 8]


def test_ui_serves_on_localhost_only(tmp_path: Path) -> None:
    port = _free_port()
    env = {
        **os.environ,
        "IFTYEDITS_HOME": str(ROOT),
        "IFTYEDITS_DATA_DIR": str(tmp_path / "data"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "iftyedits.cli", "ui", "--no-worker", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_stcore/health", timeout=2
                ) as response:
                    status = response.status
                    break
            except OSError:
                time.sleep(0.5)
        assert status == 200, "Streamlit did not become healthy on 127.0.0.1"
        addresses = _listening_addresses(port)
        if sys.platform == "darwin":
            assert addresses, "lsof found no listener; bind address unverified"
        if addresses:
            assert all(a.startswith("127.0.0.1:") for a in addresses), addresses
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
