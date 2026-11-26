"""FFmpeg adapter without FFmpeg: parsing, encoder choice and process control.

Process-control tests use small shell scripts standing in for ffmpeg, so they run
on machines (and CI runners) without FFmpeg installed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from iftyedits.adapters.ffmpeg import (
    FFmpeg,
    choose_h264,
    detect_capabilities,
    parse_encoders,
    parse_filters,
    parse_progress_seconds,
)
from iftyedits.core.errors import ExternalToolError, JobCancelledError

ENCODERS = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC
 V....D h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)
 VFS..D mjpeg                MJPEG (Motion JPEG)
 A....D aac                  AAC (Advanced Audio Coding)
 A....D pcm_s16le            PCM signed 16-bit little-endian
"""
FILTERS = """Filters:
  T.. = Timeline support
 .. scale             V->V       Scale the input video size.
 .. pad               V->V       Pad the input video.
 .. setsar            V->V       Set the pixel sample aspect ratio.
 .. format            V->V       Convert the input video to one of the specified pixel formats.
 TS thumbnail         V->V       Select the most representative frame.
"""


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _cancel_when_started(pid_file: Path) -> threading.Event:
    """Set the returned event once the fake process has written its pid.

    Waiting for the process (rather than a fixed delay) keeps the test reliable when
    macOS takes a moment to vet a newly created executable.
    """
    cancel = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    threading.Thread(target=watch, daemon=True).start()
    return cancel


def _ffmpeg(path: Path, **kwargs: float) -> FFmpeg:
    return FFmpeg(str(path), poll_interval_s=0.02, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ parsing


def test_parse_encoders_and_filters() -> None:
    assert parse_encoders(ENCODERS) == {"libx264", "h264_videotoolbox", "mjpeg", "aac", "pcm_s16le"}
    assert parse_filters(FILTERS) == {"scale", "pad", "setsar", "format", "thumbnail"}
    assert parse_encoders("") == frozenset()


@pytest.mark.parametrize(
    ("line", "seconds"),
    [
        ("out_time_us=1500000", 1.5),
        ("out_time_ms=2000000\n", 2.0),
        ("out_time_us=N/A", None),
        ("out_time=00:00:01.500000", None),
        ("progress=continue", None),
        ("out_time_us=-5", 0.0),
    ],
)
def test_parse_progress_seconds(line: str, seconds: float | None) -> None:
    assert parse_progress_seconds(line) == seconds


def test_choose_h264_prefers_verified_videotoolbox() -> None:
    both = frozenset({"h264_videotoolbox", "libx264"})
    works = {"h264_videotoolbox": True, "libx264": True}
    hw = choose_h264(both, "auto", works)
    assert hw is not None and (hw.name, hw.hardware) == ("h264_videotoolbox", True)

    forced = choose_h264(both, "libx264", works)
    assert forced is not None and (forced.name, forced.hardware) == ("libx264", False)

    broken = choose_h264(both, "auto", {"h264_videotoolbox": False, "libx264": True})
    assert broken is not None and broken.name == "libx264"
    assert "failed a test encode" in broken.reason

    missing = choose_h264(frozenset({"libx264"}), "h264_videotoolbox", {"libx264": True})
    assert missing is not None and missing.name == "libx264"
    assert "not available" in missing.reason

    assert choose_h264(frozenset({"mpeg4"}), "auto", {}) is None
    assert choose_h264(both, "auto", {}) is None  # listed but never verified


# ------------------------------------------------------------------ detection


def test_missing_ffmpeg_binary() -> None:
    with pytest.raises(ExternalToolError, match="ffmpeg not found"):
        FFmpeg("definitely-not-an-ffmpeg-binary")


def test_detect_capabilities_verifies_hardware_encoder(tmp_path: Path) -> None:
    (tmp_path / "enc.txt").write_text(ENCODERS)
    (tmp_path / "fil.txt").write_text(FILTERS.replace(" TS thumbnail", " TS thumbnailx"))
    binary = _script(
        tmp_path,
        "fake-ffmpeg",
        f"""case "$*" in
  *-version*) echo "ffmpeg version 9.9-test"; exit 0;;
  *-encoders*) cat "{tmp_path}/enc.txt"; exit 0;;
  *-filters*) cat "{tmp_path}/fil.txt"; exit 0;;
  *h264_videotoolbox*) echo "cannot create compression session" >&2; exit 1;;
  *) exit 0;;
esac
""",
    )
    caps = detect_capabilities(str(binary), "auto")
    assert caps.version == "ffmpeg version 9.9-test"
    assert caps.h264 is not None
    assert (caps.h264.name, caps.h264.hardware) == ("libx264", False)
    assert "failed a test encode" in caps.h264.reason
    assert caps.missing == ("filter thumbnail",)


# ------------------------------------------------------------------ run()


def test_run_reports_progress(tmp_path: Path) -> None:
    binary = _script(
        tmp_path,
        "ok",
        "echo out_time_us=500000\nsleep 0.1\necho out_time_us=1000000\necho progress=end\n",
    )
    seen: list[float] = []
    _ffmpeg(binary).run(["-i", "in.mp4", "out.wav"], progress=seen.append)
    assert seen and seen[-1] == 1.0
    assert seen == sorted(seen)


def test_run_failure_redacts_paths(tmp_path: Path) -> None:
    secret = tmp_path / "private folder" / "Interview.mp4"
    binary = _script(tmp_path, "bad", f'echo "{secret}: Invalid data found" >&2\nexit 1\n')
    with pytest.raises(ExternalToolError) as info:
        _ffmpeg(binary).run(["-i", str(secret)], redact=[secret])
    message = str(info.value)
    assert "exit 1" in message and "Interview.mp4: Invalid data found" in message
    assert "private folder" not in message


def test_run_cancel_stops_the_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    binary = _script(tmp_path, "slow", f'echo $$ > "{pid_file}"\nexec sleep 30\n')
    cancel = _cancel_when_started(pid_file)
    started = time.monotonic()
    with pytest.raises(JobCancelledError):
        _ffmpeg(binary).run([], cancel=cancel)
    assert time.monotonic() - started < 30
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_run_kills_process_that_ignores_sigterm(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    binary = _script(
        tmp_path,
        "stubborn",
        f'trap "" TERM\necho $$ > "{pid_file}"\nwhile true; do sleep 0.05; done\n',
    )
    cancel = _cancel_when_started(pid_file)
    with pytest.raises(JobCancelledError):
        _ffmpeg(binary, terminate_grace_s=0.3).run([], cancel=cancel)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_run_timeout(tmp_path: Path) -> None:
    binary = _script(tmp_path, "hang", "exec sleep 30\n")
    started = time.monotonic()
    with pytest.raises(ExternalToolError, match="timed out"):
        _ffmpeg(binary).run([], timeout_s=0.3)
    assert time.monotonic() - started < 10


def test_run_cancelled_before_start_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    binary = _script(tmp_path, "mark", f'touch "{marker}"\n')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        _ffmpeg(binary).run([], cancel=cancel)
    assert not marker.exists()
