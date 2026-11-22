"""Display helpers for the Short editor page. No I/O and no business logic."""

from __future__ import annotations

from typing import Any

from iftyedits.storage.shorts import KIND_EXPORT, ShortRender
from iftyedits.ui.formatting import human_size, utc_short

CAPTION_PRESET_LABELS = {
    "classic": "Classic (white, outlined)",
    "boxed": "Boxed (white on dark box)",
    "highlight": "Highlight (active word colored)",
    "minimal": "Minimal (smaller, thin outline)",
    "impact": "Impact (large, cyan active word)",
    "studio": "Studio (soft box, violet active word)",
    "clean": "Clean creator (white, key words in yellow)",
    "punchy": "Bold punchy (large, key words in pink)",
    "cinematic": "Cinematic (small, light, low on screen)",
    "mobile": "High contrast mobile (large on a solid box)",
}
SIZE_LABELS = {
    "1080p": "1080 x 1920 (recommended)",
    "720p": "720 x 1280",
    "540p": "540 x 960 (small)",
}
QUALITY_LABELS = {"high": "High", "balanced": "Balanced", "small": "Smaller file"}
STATUS_LABELS = {
    "queued": "Queued",
    "running": "Rendering",
    "succeeded": "Finished",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
TIMING_NOTES = {
    "word": "Word timing from the transcript.",
    "segment": "Segment timing only (no per-word timing in this transcript).",
    "estimated": "Timing estimated from the corrected text.",
}


def clock(seconds: float) -> str:
    """``M:SS.s`` for clip boundaries."""
    total = max(0.0, seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes}:{secs:04.1f}"


def render_row(render: ShortRender) -> dict[str, Any]:
    """One row of the render history table."""
    detail = ""
    if render.status == "succeeded" and render.width and render.height:
        detail = f"{render.width}x{render.height}, {render.duration_s or 0:.1f}s"
        if render.size_bytes:
            detail += f", {human_size(render.size_bytes)}"
    elif render.error:
        detail = render.error
    return {
        "Id": render.id,
        "Kind": "Export" if render.kind == KIND_EXPORT else "Preview",
        "Status": STATUS_LABELS.get(render.status, render.status),
        "Range": f"{clock(render.config.start_s)} - {clock(render.config.end_s)}",
        "Result": detail,
        "Created (UTC)": utc_short(render.created_at),
    }
