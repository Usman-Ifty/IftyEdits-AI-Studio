"""yt-dlp adapter: the only code that talks to YouTube.

yt-dlp runs as a subprocess (argument list, no shell, hard timeouts). No cookies, logins or
browser profiles are passed, so only videos that are publicly watchable without signing in
can be fetched. Nothing here tries to get around DRM, age gates, paywalls or private videos;
those fail with a clear message.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iftyedits.core.errors import ExternalToolError, JobCancelledError, SourceDownloadError
from iftyedits.core.sources import SourceMetadata, VideoUrl

logger = logging.getLogger(__name__)

_PROGRESS_PREFIX = "IFTYPROGRESS "
_FORMAT = (
    "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/"
    "bv*[height<=1080]+ba/b[height<=1080]/b"
)
_TAIL = 600
_REFUSALS: tuple[tuple[str, str], ...] = (
    ("private video", "This video is private. Only videos viewable without signing in work."),
    ("sign in to confirm your age", "This video is age-restricted and cannot be fetched."),
    ("age-restricted", "This video is age-restricted and cannot be fetched."),
    ("members-only", "This video is for channel members only and cannot be fetched."),
    ("members only", "This video is for channel members only and cannot be fetched."),
    ("requires payment", "This video requires payment and cannot be fetched."),
    ("drm", "This video is DRM-protected and cannot be fetched."),
    ("video unavailable", "This video is unavailable (removed, private or region-locked)."),
    ("not available", "This video is not available to download."),
    ("live event will begin", "This live stream has not started yet."),
    ("sign in to confirm you", "YouTube asked to sign in, so this video cannot be fetched."),
    ("unable to download", "Could not reach YouTube. Check the connection and try again."),
    ("urlopen error", "Could not reach YouTube. Check the connection and try again."),
    ("timed out", "The connection to YouTube timed out. Try again."),
)


def explain_failure(stderr: str) -> str:
    """Turn yt-dlp's error output into one short, actionable sentence."""
    lowered = stderr.lower()
    for needle, message in _REFUSALS:
        if needle in lowered:
            return message
    tail = stderr.strip().splitlines()[-1:] or ["unknown error"]
    return f"Could not fetch the video: {tail[0][:_TAIL]}"


def parse_metadata(text: str, url: VideoUrl) -> SourceMetadata:
    """Read the JSON printed by ``yt-dlp --dump-single-json``."""
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceDownloadError("YouTube returned information that could not be read.") from exc
    if data.get("_type") in {"playlist", "multi_video"}:
        raise SourceDownloadError("That link is a playlist. Paste the link of one video.")
    duration = data.get("duration")
    return SourceMetadata(
        source_key=url.source_key,
        url=url.canonical,
        title=str(data.get("title") or url.video_id),
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        duration_s=float(duration) if isinstance(duration, int | float) else None,
        upload_date=str(data["upload_date"]) if data.get("upload_date") else None,
        license=str(data["license"]) if data.get("license") else None,
        is_live=bool(data.get("is_live") or data.get("live_status") in {"is_live", "is_upcoming"}),
        description=str(data.get("description") or "")[:2000],
    )


class YtDlpDownloader:
    """Implements :class:`iftyedits.core.protocols.SourceDownloader` with yt-dlp."""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        timeout_s: float = 3600.0,
        metadata_timeout_s: float = 90.0,
        max_filesize_mb: int = 4096,
    ) -> None:
        self.timeout_s = timeout_s
        self.metadata_timeout_s = metadata_timeout_s
        self.max_filesize_mb = max_filesize_mb
        resolved = shutil.which(ffmpeg_path)
        self.ffmpeg = resolved

    def _base(self) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-cache-dir",
            "--no-warnings",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
        ]
        if self.ffmpeg:
            args += ["--ffmpeg-location", str(Path(self.ffmpeg).parent)]
        return args

    def fetch_metadata(self, url: VideoUrl) -> SourceMetadata:
        """Ask YouTube about the video (nothing is downloaded)."""
        args = [*self._base(), "--dump-single-json", "--skip-download", url.canonical]
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args, capture_output=True, text=True, timeout=self.metadata_timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceDownloadError("YouTube did not answer in time. Try again.") from exc
        except OSError as exc:
            raise ExternalToolError(f"yt-dlp could not run: {exc}") from exc
        if done.returncode != 0:
            raise SourceDownloadError(explain_failure(done.stderr))
        return parse_metadata(done.stdout, url)

    def download(
        self,
        url: VideoUrl,
        directory: Path,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """Download the video as MP4 (up to 1080p) into ``directory``."""
        directory.mkdir(parents=True, exist_ok=True)
        template = str(directory / f"{url.video_id}.%(ext)s")
        args = [
            *self._base(),
            "--newline",
            "--no-mtime",
            "--max-filesize",
            f"{self.max_filesize_mb}M",
            "-f",
            _FORMAT,
            "--merge-output-format",
            "mp4",
            "--progress-template",
            f"download:{_PROGRESS_PREFIX}%(progress._percent_str)s",
            "-o",
            template,
            url.canonical,
        ]
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise ExternalToolError(f"yt-dlp could not run: {exc}") from exc
        timer = threading.Timer(self.timeout_s, proc.kill)
        timer.start()
        errors: list[str] = []
        collector = threading.Thread(
            target=lambda: errors.extend(proc.stderr.readlines()) if proc.stderr else None,
            daemon=True,
        )
        collector.start()
        try:
            for line in proc.stdout or ():
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise JobCancelledError("Download cancelled")
                if line.startswith(_PROGRESS_PREFIX) and progress is not None:
                    text = line[len(_PROGRESS_PREFIX) :].strip().rstrip("%")
                    try:
                        fraction = max(0.0, min(1.0, float(text) / 100.0))
                    except ValueError:
                        continue
                    progress(fraction, f"Downloading {fraction:.0%}")
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            collector.join(timeout=5)
        if code != 0:
            raise SourceDownloadError(explain_failure("".join(errors)))
        files = sorted(
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(".part")
        )
        if not files:
            leftovers = [p for p in directory.iterdir() if p.is_file()]
            if leftovers:
                return max(leftovers, key=lambda p: p.stat().st_size)
            raise SourceDownloadError("The download finished but produced no video file.")
        return files[0]
