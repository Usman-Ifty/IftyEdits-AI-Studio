"""Fetch a video from a pasted link into the protected vault.

The download goes to a private folder under ``data/staging/`` and is imported through the
normal ingest path (hashing, vault copy, probe), so a URL video is an ordinary original
afterwards. Pasting the same link again, or a link whose bytes match an existing original,
reuses what is stored instead of keeping two copies.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core.errors import JobCancelledError, SourceDownloadError
from iftyedits.core.models import ImportOutcome, Video
from iftyedits.core.protocols import SourceDownloader
from iftyedits.core.sources import SourceMetadata, parse_video_url
from iftyedits.services.ingest import IngestService
from iftyedits.storage.video_urls import VideoUrlRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
DOWNLOAD_SHARE = 0.9
"""Share of the progress bar for the download; the rest is hashing and storing."""


@dataclass(frozen=True, slots=True)
class UrlImport:
    """What fetching a link produced."""

    video: Video
    metadata: SourceMetadata | None
    reused: bool
    """True when the link (or its bytes) was already stored and nothing was downloaded."""
    outcome: ImportOutcome


def safe_name(title: str, video_id: str) -> str:
    """Readable, filesystem-safe file name for a downloaded video."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")[:60].strip("-")
    return f"{slug or 'video'}-{video_id}.mp4"


class UrlIngestService:
    """Validates a link, downloads it and hands the file to :class:`IngestService`."""

    def __init__(
        self,
        settings: Settings,
        ingest: IngestService,
        videos: VideoRepository,
        urls: VideoUrlRepository,
        downloader: SourceDownloader,
    ) -> None:
        self.settings = settings
        self.ingest = ingest
        self.videos = videos
        self.urls = urls
        self.downloader = downloader

    def fetch(
        self,
        text: str,
        *,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> UrlImport:
        """Import the video behind ``text``. Raises domain errors with readable messages."""
        report = progress or (lambda _f, _m: None)
        url = parse_video_url(text)
        known = self.urls.get(url.source_key)
        if known is not None and known.video_id is not None:
            video = self.videos.get(known.video_id)
            if video is not None:
                report(1.0, "Already downloaded; using the stored original")
                return UrlImport(video, None, True, ImportOutcome.DUPLICATE)
        report(0.02, "Checking the link")
        meta = self.downloader.fetch_metadata(url)
        if meta.is_live:
            raise SourceDownloadError("Live streams cannot be fetched. Wait until it has ended.")
        limit = self.settings.url_max_duration_s
        if meta.duration_s is not None and meta.duration_s > limit:
            raise SourceDownloadError(
                f"This video is {meta.duration_s / 3600:.1f} h long; the limit is "
                f"{limit / 3600:.1f} h (IFTYEDITS_URL_MAX_DURATION_S)."
            )
        self.urls.start(meta)
        folder = self.settings.staging_dir / f"url-{uuid.uuid4().hex[:10]}"
        try:
            folder.mkdir(parents=True)

            def on_download(fraction: float, message: str) -> None:
                report(0.05 + fraction * (DOWNLOAD_SHARE - 0.05), message)

            downloaded = self.downloader.download(url, folder, progress=on_download, cancel=cancel)
            named = folder / safe_name(meta.title, url.video_id)
            downloaded.rename(named)
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Download cancelled")
            report(DOWNLOAD_SHARE, "Storing the original")

            def on_copy(copied: int, total: int) -> None:
                if cancel is not None and cancel.is_set():
                    raise JobCancelledError("Import cancelled; the partial copy was removed")
                if total:
                    report(DOWNLOAD_SHARE + 0.1 * copied / total, "Storing the original")

            result = self.ingest.import_download(named, progress=on_copy)
        except JobCancelledError:
            raise
        except Exception as exc:
            self.urls.mark_failed(url.source_key, str(exc))
            raise
        finally:
            self._remove(folder)
        self.urls.mark_downloaded(url.source_key, result.video.id)
        report(1.0, "Original stored")
        return UrlImport(result.video, meta, False, result.outcome)

    def _remove(self, folder: Path) -> None:
        """Delete this download's own staging folder (never anything else)."""
        staging = self.settings.staging_dir.resolve()
        try:
            target = folder.resolve()
        except OSError:
            return
        if target.parent == staging and target.name.startswith("url-") and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
