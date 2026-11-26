"""Display formatting for the Analytics page (no I/O)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from iftyedits.core.analytics import Comparison
from iftyedits.storage.analytics import TrackedVideo
from iftyedits.ui.formatting import utc_short


def _dash(value: object) -> object:
    return "-" if value is None or value == "" else value


def video_rows(videos: Sequence[TrackedVideo]) -> list[dict[str, Any]]:
    """One table row per followed video. Unknown numbers show a dash, never zero."""
    rows: list[dict[str, Any]] = []
    for v in videos:
        window = f"{v.window_start} to {v.window_end}" if v.window_start else None
        percent = None if v.avg_view_percent is None else f"{v.avg_view_percent:.0f}%"
        rows.append(
            {
                "Video": v.title or v.project_title or v.video_id,
                "Views": _dash(v.views),
                "Likes": _dash(v.likes),
                "Comments": _dash(v.comments),
                "Avg watched": _dash(percent),
                "Watch minutes": _dash(None if v.watch_minutes is None else round(v.watch_minutes)),
                "Subscribers gained": _dash(v.subscribers_gained),
                "Window": _dash(window),
                "Visibility": _dash(v.privacy),
                "Refreshed (UTC)": utc_short(v.fetched_at) if v.fetched_at else "never",
                "Link": f"https://www.youtube.com/watch?v={v.video_id}",
            }
        )
    # one type per column keeps the table renderer happy
    return [{key: str(value) for key, value in row.items()} for row in rows]


def comparison_rows(comparison: Comparison) -> list[dict[str, str]]:
    """Table rows of one comparison."""
    return [
        {
            "Group": g.group,
            "Videos": str(g.videos),
            "Median views": "-" if g.median_views is None else f"{g.median_views:,.0f}",
            "Median watched": (
                "-" if g.median_view_percent is None else f"{g.median_view_percent:.0f}%"
            ),
            "Likes per 100 views": (
                "-" if g.likes_per_100_views is None else f"{g.likes_per_100_views:g}"
            ),
        }
        for g in comparison.groups
    ]
