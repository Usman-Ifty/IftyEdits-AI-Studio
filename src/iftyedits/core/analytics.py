"""Performance numbers of uploaded Shorts: parsing and honest comparisons (pure, no I/O).

Two Google sources are used. The Data API (``videos.list``) gives views, likes and comments
with the permission the studio already asks for. The YouTube Analytics API (``reports.query``)
adds watch time and retention and needs its own, optional, permission.

Comparisons are descriptive only: they show how many videos each figure rests on, say nothing
below :data:`MIN_GROUP` videos per group, and never claim that one choice *caused* a result.
Nothing here changes ranking, publishing or anything else automatically.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
REPORT_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
)
MAX_REPORT_VIDEOS = 200
"""The per-video report returns at most this many rows."""
MAX_LIST_IDS = 50
"""Video ids sent per ``videos.list`` call."""
MIN_GROUP = 5
"""Fewer videos than this in a group and it is not compared."""


@dataclass(frozen=True, slots=True)
class VideoDetails:
    """What the Data API says about one video."""

    video_id: str
    channel_id: str
    title: str
    privacy: str
    published_at: str
    views: int | None
    likes: int | None
    comments: int | None


@dataclass(frozen=True, slots=True)
class ReportRow:
    """What the Analytics API says about one video inside a date window."""

    video_id: str
    views: int | None = None
    watch_minutes: float | None = None
    avg_view_seconds: float | None = None
    avg_view_percent: float | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    subscribers_gained: int | None = None


@dataclass(frozen=True, slots=True)
class Sample:
    """One video's numbers with the editing choices behind it."""

    video_id: str
    views: int | None
    likes: int | None
    avg_view_percent: float | None
    attributes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GroupStat:
    """Medians of one group of videos."""

    group: str
    videos: int
    median_views: float | None
    median_view_percent: float | None
    likes_per_100_views: float | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """Videos split by one editing choice."""

    attribute: str
    groups: tuple[GroupStat, ...]
    comparable: bool
    """True when at least two groups each hold :data:`MIN_GROUP` videos."""
    note: str


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _count(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_videos(payload: Mapping[str, Any]) -> list[VideoDetails]:
    """Videos from a ``videos.list`` reply; malformed entries are skipped."""
    found: list[VideoDetails] = []
    items = payload.get("items")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        snippet = _object(item.get("snippet"))
        status = _object(item.get("status"))
        numbers = _object(item.get("statistics"))
        found.append(
            VideoDetails(
                item["id"],
                str(snippet.get("channelId", "")),
                str(snippet.get("title", "")),
                str(status.get("privacyStatus", "")),
                str(snippet.get("publishedAt", "")),
                _count(numbers.get("viewCount")),
                _count(numbers.get("likeCount")),
                _count(numbers.get("commentCount")),
            )
        )
    return found


def parse_report(payload: Mapping[str, Any]) -> dict[str, ReportRow]:
    """Per-video rows of a ``reports.query`` reply, matched by column name.

    A reply without ``rows`` (no data yet) gives an empty result.
    """
    headers = payload.get("columnHeaders")
    rows = payload.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list):
        return {}
    names = [str(h.get("name", "")) if isinstance(h, dict) else "" for h in headers]
    if "video" not in names:
        return {}
    found: dict[str, ReportRow] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            continue
        values = dict(zip(names, row, strict=True))
        video_id = str(values["video"])
        found[video_id] = ReportRow(
            video_id,
            _count(values.get("views")),
            _number(values.get("estimatedMinutesWatched")),
            _number(values.get("averageViewDuration")),
            _number(values.get("averageViewPercentage")),
            _count(values.get("likes")),
            _count(values.get("comments")),
            _count(values.get("shares")),
            _count(values.get("subscribersGained")),
        )
    return found


def length_bucket(seconds: float | None) -> str:
    """A plain length group for a Short."""
    if seconds is None or seconds <= 0:
        return ""
    if seconds < 25:
        return "under 25 s"
    if seconds <= 45:
        return "25 to 45 s"
    return "over 45 s"


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def compare(samples: Sequence[Sample], attribute: str) -> Comparison:
    """Group videos by one attribute and report medians with their sample sizes."""
    groups: dict[str, list[Sample]] = {}
    for sample in samples:
        key = sample.attributes.get(attribute, "")
        if key:
            groups.setdefault(key, []).append(sample)
    stats: list[GroupStat] = []
    for key, members in groups.items():
        views = [float(m.views) for m in members if m.views is not None]
        percent = [m.avg_view_percent for m in members if m.avg_view_percent is not None]
        liked = [(m.likes, m.views) for m in members if m.likes is not None and m.views]
        total_views = sum(v for _, v in liked)
        stats.append(
            GroupStat(
                key,
                len(members),
                _median(views),
                _median(percent),
                round(100.0 * sum(n for n, _ in liked) / total_views, 2) if total_views else None,
            )
        )
    stats.sort(key=lambda s: (-s.videos, s.group))
    large = [s for s in stats if s.videos >= MIN_GROUP and s.median_views is not None]
    if len(large) < 2:
        note = (
            f"Not enough data to compare: at least two groups need {MIN_GROUP} or more videos each."
        )
        return Comparison(attribute, tuple(stats), False, note)
    best = max(large, key=lambda s: s.median_views or 0.0)
    note = (
        f"'{best.group}' has the highest median views ({best.median_views:,.0f}, "
        f"{best.videos} videos) among groups with enough videos. This is a pattern in a small "
        "sample, not proof that the choice caused it."
    )
    return Comparison(attribute, tuple(stats), True, note)
