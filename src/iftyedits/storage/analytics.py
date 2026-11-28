"""Tracked YouTube videos and the numbers fetched for them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from iftyedits.core.analytics import ReportRow, VideoDetails
from iftyedits.storage.db import utc_now


@dataclass(frozen=True, slots=True)
class TrackedVideo:
    """A video whose performance is followed, with its latest snapshot if there is one."""

    video_id: str
    project_id: int | None
    source: str
    added_at: str
    project_title: str
    category: str
    caption_config: str
    duration_s: float | None
    fetched_at: str | None
    title: str
    privacy: str
    published_at: str
    views: int | None
    likes: int | None
    comments: int | None
    window_start: str | None
    window_end: str | None
    watch_minutes: float | None
    avg_view_seconds: float | None
    avg_view_percent: float | None
    shares: int | None
    subscribers_gained: int | None
    traffic: tuple[tuple[str, int], ...] = ()
    """``(source, views)`` of the latest snapshot, most views first."""


def _traffic(text: str | None) -> tuple[tuple[str, int], ...]:
    try:
        return tuple((str(source), int(views)) for source, views in json.loads(text or "[]"))
    except (ValueError, TypeError):
        return ()


class AnalyticsRepository:
    """Reads and writes the analytics tables."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def track(self, video_id: str, project_id: int | None, source: str) -> bool:
        """Start following a video; returns False if it was already followed.

        A later call that knows the Short fills in a missing link.
        """
        with self.conn:
            created = self.conn.execute(
                "INSERT OR IGNORE INTO tracked_videos "
                "(youtube_video_id, project_id, source, added_at) VALUES (?, ?, ?, ?)",
                (video_id, project_id, source, utc_now()),
            ).rowcount
            if not created and project_id is not None:
                self.conn.execute(
                    "UPDATE tracked_videos SET project_id = ? "
                    "WHERE youtube_video_id = ? AND project_id IS NULL",
                    (project_id, video_id),
                )
        return bool(created)

    def untrack(self, video_id: str) -> bool:
        """Stop following a video and forget its snapshots (nothing on YouTube changes)."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM analytics_snapshots WHERE youtube_video_id = ?", (video_id,)
            )
            return bool(
                self.conn.execute(
                    "DELETE FROM tracked_videos WHERE youtube_video_id = ?", (video_id,)
                ).rowcount
            )

    def ids(self) -> list[str]:
        """Followed video ids, oldest first."""
        rows = self.conn.execute(
            "SELECT youtube_video_id FROM tracked_videos ORDER BY added_at, youtube_video_id"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def uploaded(self) -> list[tuple[str, int]]:
        """``(video id, project id)`` of every finished upload made by the studio."""
        rows = self.conn.execute(
            "SELECT youtube_video_id, project_id FROM youtube_uploads "
            "WHERE status = 'uploaded' AND youtube_video_id IS NOT NULL ORDER BY id"
        ).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

    def add_snapshot(
        self,
        details: VideoDetails,
        report: ReportRow | None,
        window: tuple[str, str] | None,
        traffic: Sequence[tuple[str, int]] = (),
    ) -> None:
        """Store what Google reported now."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO analytics_snapshots (youtube_video_id, fetched_at, title, privacy, "
                "published_at, views, likes, comments, window_start, window_end, watch_minutes, "
                "avg_view_seconds, avg_view_percent, shares, subscribers_gained, traffic_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    details.video_id,
                    utc_now(),
                    details.title,
                    details.privacy,
                    details.published_at,
                    details.views,
                    details.likes,
                    details.comments,
                    window[0] if window and (report or traffic) else None,
                    window[1] if window and (report or traffic) else None,
                    report.watch_minutes if report else None,
                    report.avg_view_seconds if report else None,
                    report.avg_view_percent if report else None,
                    report.shares if report else None,
                    report.subscribers_gained if report else None,
                    json.dumps(list(traffic)) if traffic else "",
                ),
            )

    def snapshot_count(self, video_id: str) -> int:
        """How many snapshots a video has."""
        row = self.conn.execute(
            "SELECT count(*) FROM analytics_snapshots WHERE youtube_video_id = ?", (video_id,)
        ).fetchone()
        return int(row[0])

    def latest(self) -> list[TrackedVideo]:
        """Every followed video with its newest snapshot, newest addition first."""
        rows = self.conn.execute(
            """
            SELECT t.youtube_video_id, t.project_id, t.source, t.added_at,
                   p.title AS project_title, p.config_json AS config_json,
                   (SELECT c.category FROM clip_candidates c
                     WHERE c.candidate_key = p.candidate_key
                     ORDER BY c.id DESC LIMIT 1) AS category,
                   (SELECT r.duration_s FROM short_renders r
                     WHERE r.project_id = p.id AND r.status = 'succeeded'
                     ORDER BY r.id DESC LIMIT 1) AS duration_s,
                   s.fetched_at, s.title, s.privacy, s.published_at, s.views, s.likes,
                   s.comments, s.window_start, s.window_end, s.watch_minutes,
                   s.avg_view_seconds, s.avg_view_percent, s.shares, s.subscribers_gained,
                   s.traffic_json
              FROM tracked_videos t
              LEFT JOIN short_projects p ON p.id = t.project_id
              LEFT JOIN analytics_snapshots s ON s.id = (
                   SELECT max(id) FROM analytics_snapshots
                    WHERE youtube_video_id = t.youtube_video_id)
             ORDER BY t.added_at DESC, t.youtube_video_id
            """
        ).fetchall()
        return [
            TrackedVideo(
                video_id=r["youtube_video_id"],
                project_id=r["project_id"],
                source=r["source"],
                added_at=r["added_at"],
                project_title=r["project_title"] or "",
                category=r["category"] or "",
                caption_config=r["config_json"] or "",
                duration_s=r["duration_s"],
                fetched_at=r["fetched_at"],
                title=r["title"] or "",
                privacy=r["privacy"] or "",
                published_at=r["published_at"] or "",
                views=r["views"],
                likes=r["likes"],
                comments=r["comments"],
                window_start=r["window_start"],
                window_end=r["window_end"],
                watch_minutes=r["watch_minutes"],
                avg_view_seconds=r["avg_view_seconds"],
                avg_view_percent=r["avg_view_percent"],
                shares=r["shares"],
                subscribers_gained=r["subscribers_gained"],
                traffic=_traffic(r["traffic_json"]),
            )
            for r in rows
        ]
