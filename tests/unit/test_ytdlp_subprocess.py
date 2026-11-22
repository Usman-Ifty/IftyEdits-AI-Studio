"""Real subprocess handling of the yt-dlp adapter: cancel, timeout and cleanup (no network)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from iftyedits.adapters.ytdlp import YtDlpDownloader
from iftyedits.core.errors import JobCancelledError, SourceDownloadError
from iftyedits.core.sources import parse_video_url

URL = parse_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

# Stands in for yt-dlp: records its pid, reports progress, then hangs like a stalled download.
HANGING = """
import os, sys, time
open(os.environ['FAKE_PID_FILE'], 'w').write(str(os.getpid()))
for pct in ('10.0%', '20.0%'):
    print(pct, flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _downloader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: float
) -> YtDlpDownloader:
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(HANGING)
    monkeypatch.setenv("FAKE_PID_FILE", str(tmp_path / "pid"))
    dl = YtDlpDownloader(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(dl, "_base", lambda: [sys.executable, str(script)])
    return dl


def test_cancel_kills_the_download_process_and_leaves_no_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dl = _downloader(monkeypatch, tmp_path)
    cancel = threading.Event()

    out = tmp_path / "dl"
    # the fake goes silent like a stalled download: cancel must still stop it quickly
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        dl.download(URL, out, cancel=cancel)
    assert time.monotonic() - started < 10  # not the 60 s the fake would hang for
    pid = int((tmp_path / "pid").read_text())
    assert not _alive(pid)
    assert not [p for p in out.iterdir() if p.suffix == ".mp4"]


def test_a_stalled_download_is_killed_at_the_timeout_with_a_plain_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dl = _downloader(monkeypatch, tmp_path, timeout_s=1.5)
    with pytest.raises(SourceDownloadError) as info:
        dl.download(URL, tmp_path / "dl")
    pid = int((tmp_path / "pid").read_text())
    assert not _alive(pid)
    assert "Traceback" not in str(info.value) and str(tmp_path) not in str(info.value)
