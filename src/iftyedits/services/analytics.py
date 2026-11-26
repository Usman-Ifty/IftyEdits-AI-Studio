"""Follow uploaded Shorts and fetch their numbers. Read only: nothing on YouTube changes.

Numbers are fetched only when the owner asks. Basic figures (views, likes, comments) use the
permission the upload connection already has. Watch time and retention need the optional
Analytics permission, granted on a separate Google consent screen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from iftyedits.config import Settings
from iftyedits.core.analytics import (
    ANALYTICS_SCOPE,
    MAX_LIST_IDS,
    MAX_REPORT_VIDEOS,
    REPORT_METRICS,
    Comparison,
    ReportRow,
    Sample,
    VideoDetails,
    compare,
    length_bucket,
    parse_report,
    parse_videos,
)
from iftyedits.core.errors import AnalyticsError, IftyEditsError, InvalidSourceError
from iftyedits.core.shorts import ShortConfig
from iftyedits.core.sources import parse_video_url
from iftyedits.services.youtube_upload import YouTubeUploadService, build_youtube_service
from iftyedits.storage.analytics import AnalyticsRepository, TrackedVideo

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
DEFAULT_DAYS = 28
ATTRIBUTES = {
    "category": "Moment type",
    "captions": "Caption style",
    "length": "Length",
    "framing": "Framing",
}


@dataclass(frozen=True, slots=True)
class AnalyticsStatus:
    """What can be fetched right now. Makes no network call."""

    connected: bool
    detailed: bool
    """True when the Analytics permission was granted."""
    problem: str = ""


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did."""

    updated: int
    skipped: tuple[tuple[str, str], ...]
    """``(video id, reason)``."""
    detailed: int
    """Videos that also received watch time and retention."""
    window: tuple[str, str]
    note: str


def video_id_from(reference: str) -> str:
    """A YouTube video id from an id or a link; raises :class:`AnalyticsError`."""
    text = reference.strip()
    if _VIDEO_ID.fullmatch(text):
        return text
    try:
        return parse_video_url(text).video_id
    except (InvalidSourceError, IftyEditsError, ValueError) as exc:
        raise AnalyticsError("Enter a YouTube video link or its 11-character video id.") from exc


def attributes_of(video: TrackedVideo) -> dict[str, str]:
    """The editing choices behind a linked Short (empty values are not compared)."""
    found = {"category": video.category, "length": length_bucket(video.duration_s)}
    if video.caption_config:
        try:
            config = ShortConfig.from_dict(json.loads(video.caption_config))
        except (IftyEditsError, ValueError, TypeError, KeyError):
            return found
        found["captions"] = config.captions.preset if config.captions.enabled else "no captions"
        found["framing"] = "moving crop" if config.crop_path else "steady crop"
    return found


class AnalyticsService:
    """Use cases for the Analytics page and the CLI."""

    def __init__(
        self,
        youtube: YouTubeUploadService,
        repo: AnalyticsRepository,
        *,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.youtube = youtube
        self.repo = repo
        self._today = today

    def status(self) -> AnalyticsStatus:
        """Whether YouTube is connected and whether detailed numbers are allowed."""
        connection = self.youtube.connection()
        if not connection.configured or not connection.connected:
            return AnalyticsStatus(False, False, connection.problem or "YouTube is not connected.")
        try:
            scopes = self.youtube.client().granted_scopes()
        except IftyEditsError as exc:
            return AnalyticsStatus(False, False, str(exc))
        return AnalyticsStatus(True, ANALYTICS_SCOPE in scopes)

    def connect_detailed(self, on_url: Callable[[str], None] | None = None) -> None:
        """Browser sign-in that also asks for the optional Analytics permission."""
        client = self.youtube.client()
        client.authorize(on_url=on_url, extra_scopes=(ANALYTICS_SCOPE,))

    def sync_uploads(self) -> int:
        """Follow every video the studio has uploaded. Returns how many were new."""
        return sum(
            self.repo.track(video_id, project_id, "upload")
            for video_id, project_id in self.repo.uploaded()
        )

    def add_video(self, reference: str, project_id: int | None = None) -> str:
        """Follow a video uploaded by hand, optionally linked to the Short it came from."""
        video_id = video_id_from(reference)
        if not self.repo.track(video_id, project_id, "manual") and project_id is None:
            raise AnalyticsError("That video is already being followed.")
        return video_id

    def remove_video(self, video_id: str) -> bool:
        """Stop following a video and delete its stored numbers."""
        return self.repo.untrack(video_id)

    def videos(self) -> list[TrackedVideo]:
        """Followed videos with their latest numbers."""
        return self.repo.latest()

    def comparisons(self) -> dict[str, Comparison]:
        """Descriptive comparisons over the latest numbers, keyed by display name."""
        samples = [
            Sample(v.video_id, v.views, v.likes, v.avg_view_percent, attributes_of(v))
            for v in self.repo.latest()
            if v.fetched_at is not None
        ]
        return {label: compare(samples, key) for key, label in ATTRIBUTES.items()}

    def refresh(self, days: int = DEFAULT_DAYS) -> RefreshReport:
        """Fetch current numbers for every followed video on the connected channel."""
        if not 1 <= days <= 730:
            raise AnalyticsError("Choose a window between 1 and 730 days.")
        self.sync_uploads()
        wanted = self.repo.ids()
        end = self._today()
        window = ((end - timedelta(days=days - 1)).isoformat(), end.isoformat())
        if not wanted:
            return RefreshReport(0, (), 0, window, "No videos are being followed yet.")
        status = self.status()
        if not status.connected:
            raise AnalyticsError(f"{status.problem} Connect your account in Settings.")
        client = self.youtube.client()
        channel_id, _ = client.channel()
        found: dict[str, VideoDetails] = {}
        for start in range(0, len(wanted), MAX_LIST_IDS):
            batch = wanted[start : start + MAX_LIST_IDS]
            found.update({d.video_id: d for d in parse_videos(client.video_list(batch))})
        skipped: list[tuple[str, str]] = []
        mine: list[VideoDetails] = []
        for video_id in wanted:
            details = found.get(video_id)
            if details is None:
                skipped.append((video_id, "YouTube returned no video with this id."))
            elif details.channel_id != channel_id:
                skipped.append((video_id, "This video is not on your connected channel."))
            else:
                mine.append(details)
        rows: dict[str, ReportRow] = {}
        note = ""
        if not status.detailed:
            note = (
                "Views, likes and comments only. Allow detailed analytics to add watch time "
                "and retention."
            )
        elif mine:
            ids = [d.video_id for d in mine]
            try:
                for start in range(0, len(ids), MAX_REPORT_VIDEOS):
                    batch = ids[start : start + MAX_REPORT_VIDEOS]
                    rows.update(
                        parse_report(client.analytics_report(batch, *window, REPORT_METRICS))
                    )
            except IftyEditsError as exc:
                note = f"Detailed analytics were not available: {exc}"
            if not rows and not note:
                note = (
                    "YouTube has no detailed numbers for this window yet (they arrive a few "
                    "days late)."
                )
        for details in mine:
            self.repo.add_snapshot(details, rows.get(details.video_id), window)
        return RefreshReport(len(mine), tuple(skipped), len(rows), window, note)


def build_analytics_service(settings: Settings, conn: sqlite3.Connection) -> AnalyticsService:
    """The service wired to the saved YouTube connection."""
    return AnalyticsService(build_youtube_service(settings, conn), AnalyticsRepository(conn))
