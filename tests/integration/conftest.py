"""Integration fixtures: real media generated with FFmpeg at test time.

Without ffmpeg/ffprobe these tests are skipped with an explicit reason. Set
``IFTYEDITS_REQUIRE_FFMPEG=1`` (as CI does) to turn a missing FFmpeg into a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    """Path to ffmpeg, or skip/fail depending on ``IFTYEDITS_REQUIRE_FFMPEG``."""
    if FFMPEG is None or FFPROBE is None:
        message = "ffmpeg/ffprobe not installed"
        if os.environ.get("IFTYEDITS_REQUIRE_FFMPEG") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return FFMPEG


def run_ffmpeg(ffmpeg: str, *args: str) -> None:
    """Run ffmpeg quietly; fail the test with stderr on error."""
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(f"ffmpeg failed: {completed.stderr}")


@pytest.fixture(scope="session")
def media_dir(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder of small real media files, generated once per session."""
    root = tmp_path_factory.mktemp("real_media")
    video = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    h264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]

    run_ffmpeg(ffmpeg, *video, *audio, *h264, "-c:a", "aac", "-shortest", str(root / "clip.mp4"))
    run_ffmpeg(ffmpeg, "-i", str(root / "clip.mp4"), "-c", "copy", str(root / "clip_remux.mkv"))
    run_ffmpeg(ffmpeg, *video, *h264, "-an", str(root / "silent.mov"))
    run_ffmpeg(ffmpeg, *audio, "-c:a", "aac", "-f", "mp4", str(root / "audio_only.mp4"))

    data = (root / "clip.mp4").read_bytes()
    (root / "truncated.mp4").write_bytes(data[: len(data) // 3])
    (root / "text.mp4").write_text("this is not a video\n")
    return root
