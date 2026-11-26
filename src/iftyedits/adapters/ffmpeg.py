"""FFmpeg adapter: capability detection and cancellable, timed transcodes.

Commands are argument lists (never a shell). Progress comes from ``-progress pipe:1``.
Cancellation and timeouts terminate the process (SIGTERM, then SIGKILL after a grace
period) and only report back once the process has actually exited.
"""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Literal

from iftyedits.core.errors import ExternalToolError, JobCancelledError
from iftyedits.core.models import EncoderInfo, TranscoderCapabilities
from iftyedits.core.protocols import CancelToken, SecondsCallback

logger = logging.getLogger(__name__)

EncoderPreference = Literal["auto", "h264_videotoolbox", "libx264"]
HARDWARE_H264 = "h264_videotoolbox"
SOFTWARE_H264 = "libx264"
REQUIRED_ENCODERS = ("pcm_s16le", "aac", "mjpeg")
REQUIRED_FILTERS = ("scale", "pad", "setsar", "format", "thumbnail")
_DETECT_TIMEOUT_S = 30.0
_MAX_ERROR_CHARS = 600
_STDERR_LINES = 40


def parse_encoders(text: str) -> frozenset[str]:
    """Encoder names from ``ffmpeg -encoders`` output."""
    names: set[str] = set()
    started = False
    for line in text.splitlines():
        if line.strip().startswith("------"):
            started = True
            continue
        parts = line.split()
        if started and len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


def parse_filters(text: str) -> frozenset[str]:
    """Filter names from ``ffmpeg -filters`` output."""
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


def parse_progress_seconds(line: str) -> float | None:
    """Media time from one ``-progress`` line (``out_time_us=...``), else None."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms"):  # both are microseconds in FFmpeg
        try:
            return max(0.0, int(value) / 1_000_000)
        except ValueError:
            return None
    return None


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else "unknown"


class FFmpeg:
    """Implements :class:`iftyedits.core.protocols.MediaTranscoder`."""

    def __init__(
        self,
        binary: str = "ffmpeg",
        timeout_s: float = 6 * 3600.0,
        *,
        encoder: EncoderPreference = "auto",
        poll_interval_s: float = 0.1,
        terminate_grace_s: float = 5.0,
    ) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise ExternalToolError(
                f"ffmpeg not found: {binary!r}. Install it (brew install ffmpeg) "
                "or set IFTYEDITS_FFMPEG_PATH."
            )
        self.binary = resolved
        self.timeout_s = timeout_s
        self.encoder = encoder
        self.poll_interval_s = poll_interval_s
        self.terminate_grace_s = terminate_grace_s

    # ------------------------------------------------------------------ detection

    def capabilities(self) -> TranscoderCapabilities:
        """Detected encoders and filters; hardware H.264 only if a test encode succeeds."""
        return detect_capabilities(self.binary, self.encoder)

    # ------------------------------------------------------------------ execution

    def run(
        self,
        args: Sequence[str],
        *,
        progress: SecondsCallback | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        redact: Sequence[Path] = (),
    ) -> None:
        """Run ``ffmpeg <args>``; see :class:`MediaTranscoder` for the contract."""
        limit = timeout_s if timeout_s is not None else self.timeout_s
        command = [
            self.binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            *args,
        ]
        if cancel is not None and cancel.is_set():
            raise JobCancelledError("Cancelled before FFmpeg started")
        try:
            proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise ExternalToolError(f"ffmpeg could not start: {exc}") from exc

        latest: list[float] = []
        errors: deque[str] = deque(maxlen=_STDERR_LINES)
        readers = [
            threading.Thread(target=_read_progress, args=(proc.stdout, latest), daemon=True),
            threading.Thread(target=_read_lines, args=(proc.stderr, errors), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + limit
        reported = -1.0
        outcome: Literal["exited", "cancelled", "timeout"] = "exited"
        try:
            while True:
                try:
                    proc.wait(timeout=self.poll_interval_s)
                    finished = True
                except subprocess.TimeoutExpired:
                    finished = False
                if progress is not None and latest and latest[-1] != reported:
                    reported = latest[-1]
                    progress(reported)
                if finished:
                    break
                if cancel is not None and cancel.is_set():
                    outcome = "cancelled"
                    break
                if time.monotonic() > deadline:
                    outcome = "timeout"
                    break
        finally:
            if proc.poll() is None:
                self._stop(proc)
            for reader in readers:
                reader.join(timeout=5)

        if outcome == "cancelled" or (cancel is not None and cancel.is_set()):
            raise JobCancelledError("Cancelled; FFmpeg was stopped")
        if outcome == "timeout":
            raise ExternalToolError(f"ffmpeg timed out after {limit:g}s and was stopped")
        if proc.returncode != 0:
            detail = _redact(" | ".join(errors), redact)[-_MAX_ERROR_CHARS:]
            raise ExternalToolError(
                f"ffmpeg failed (exit {proc.returncode}): {detail or 'no error output'}"
            )

    def _stop(self, proc: subprocess.Popen[str]) -> None:
        """Terminate, then kill, and wait until the process has exited."""
        proc.terminate()
        try:
            proc.wait(timeout=self.terminate_grace_s)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg did not stop after SIGTERM; killing it")
            proc.kill()
            proc.wait()


def _read_progress(stream: IO[str] | None, latest: list[float]) -> None:
    if stream is None:
        return
    for line in stream:
        seconds = parse_progress_seconds(line)
        if seconds is not None:
            latest.append(seconds)
            del latest[:-1]


def _read_lines(stream: IO[str] | None, sink: deque[str]) -> None:
    if stream is None:
        return
    for line in stream:
        text = line.strip()
        if text:
            sink.append(text)


def _redact(text: str, paths: Sequence[Path]) -> str:
    """Replace full paths with file names so errors do not expose directory layout."""
    for path in sorted(paths, key=lambda p: len(str(p)), reverse=True):
        text = text.replace(str(path), path.name)
    return text


def _run_quick(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argument list, no shell
        args, capture_output=True, text=True, timeout=_DETECT_TIMEOUT_S, check=False
    )


def encoder_works(binary: str, encoder: str) -> bool:
    """Encode three tiny frames with ``encoder``; True only if FFmpeg succeeds."""
    try:
        completed = _run_quick(
            [
                binary,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=gray:s=256x256:r=30:d=0.2",
                "-frames:v",
                "3",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ]
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def choose_h264(
    encoders: frozenset[str], preference: EncoderPreference, works: dict[str, bool]
) -> EncoderInfo | None:
    """Pick the proxy encoder. ``works`` maps encoder name to a verified test encode."""
    hw_ok = HARDWARE_H264 in encoders and works.get(HARDWARE_H264, False)
    sw_ok = SOFTWARE_H264 in encoders and works.get(SOFTWARE_H264, False)
    if preference != SOFTWARE_H264 and hw_ok:
        return EncoderInfo(HARDWARE_H264, True, "VideoToolbox verified by a test encode")
    if sw_ok:
        if preference == SOFTWARE_H264:
            reason = "software encoder selected in settings"
        elif HARDWARE_H264 in encoders:
            reason = "VideoToolbox is listed but failed a test encode"
        else:
            reason = "VideoToolbox is not available in this FFmpeg"
        return EncoderInfo(SOFTWARE_H264, False, reason)
    return None


@functools.cache
def detect_capabilities(binary: str, preference: EncoderPreference) -> TranscoderCapabilities:
    """Inspect an FFmpeg binary once per process."""
    try:
        version = _run_quick([binary, "-hide_banner", "-version"])
        enc = _run_quick([binary, "-hide_banner", "-encoders"])
        fil = _run_quick([binary, "-hide_banner", "-filters"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalToolError(f"ffmpeg could not be inspected: {exc}") from exc
    encoders = parse_encoders(enc.stdout)
    filters = parse_filters(fil.stdout)
    works = {
        name: encoder_works(binary, name)
        for name in (HARDWARE_H264, SOFTWARE_H264)
        if name in encoders
    }
    h264 = choose_h264(encoders, preference, works)
    missing = tuple(
        [f"encoder {e}" for e in REQUIRED_ENCODERS if e not in encoders]
        + [f"filter {f}" for f in REQUIRED_FILTERS if f not in filters]
        + ([] if h264 else ["a working H.264 encoder (h264_videotoolbox or libx264)"])
    )
    return TranscoderCapabilities(
        binary=binary,
        version=_first_line(version.stdout),
        encoders=encoders,
        filters=filters,
        h264=h264,
        missing=missing,
    )
