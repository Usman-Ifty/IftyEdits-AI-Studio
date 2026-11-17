"""Repository hygiene: forbidden files are ignored and never tracked."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

MUST_IGNORE = [
    ".env",
    ".env.local",
    ".venv/bin/python",
    "data/originals/ab/abc.mp4",
    "data/derived/clip.mov",
    "data/exports/short.mp4",
    "data/db/iftyedits.sqlite",
    "data/anything.txt",
    "models/whisper/model.bin",
    "models/large-v3.safetensors",
    "some/where/video.mov",
    "client_secret_123.json",
    "token.json",
]
MUST_TRACK = [".env.example", "data/originals/.gitkeep", "models/.gitkeep"]

FORBIDDEN_TRACKED = re.compile(
    r"(^|/)\.env($|\.(?!example$))"
    r"|^\.venv/"
    r"|\.(mp4|mov|mkv|wav|mp3|sqlite3?|db|bin|safetensors|pt|onnx|gguf)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None
    return subprocess.run([git, *args], cwd=ROOT, capture_output=True, text=True, check=False)


@pytest.fixture(scope="module", autouse=True)
def _require_git_repo() -> None:
    if shutil.which("git") is None or _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not inside a git repository")


@pytest.mark.parametrize("path", MUST_IGNORE)
def test_forbidden_paths_are_ignored(path: str) -> None:
    assert _git("check-ignore", "--no-index", "-q", path).returncode == 0, path


@pytest.mark.parametrize("path", MUST_TRACK)
def test_required_paths_are_not_ignored(path: str) -> None:
    assert _git("check-ignore", "--no-index", "-q", path).returncode == 1, path


def _tracked_files() -> list[str]:
    out = _git("ls-files", "--cached").stdout
    return [line for line in out.splitlines() if line]


def test_no_forbidden_files_tracked() -> None:
    offenders = [f for f in _tracked_files() if FORBIDDEN_TRACKED.search(f)]
    assert offenders == []


def test_no_secrets_in_tracked_files() -> None:
    offenders = []
    for name in _tracked_files():
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if SECRET_PATTERNS.search(text):
            offenders.append(name)
    assert offenders == []


def test_env_example_has_no_values_for_secrets() -> None:
    for line in (ROOT / ".env.example").read_text().splitlines():
        stripped = line.lstrip("# ").strip()
        if "KEY" in stripped or "SECRET" in stripped or "TOKEN" in stripped:
            name, _, value = stripped.partition("=")
            assert value == "" or value.startswith("/absolute/path"), name
