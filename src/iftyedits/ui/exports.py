"""Display rows for finished Shorts and production summaries (no I/O)."""

from __future__ import annotations

from typing import Any

from iftyedits.core.models import Job
from iftyedits.core.romanize import romanize
from iftyedits.storage.shorts import ShortRender
from iftyedits.ui.formatting import human_size
from iftyedits.ui.theme import clip_status


def export_row(title: str, render: ShortRender) -> dict[str, Any]:
    """One row for a finished or failed export."""
    ready = render.status == "succeeded"
    label, _ = clip_status("succeeded" if ready else render.status)
    size = f"{render.width}x{render.height}" if render.width and render.height else "-"
    return {
        "Short": title,
        "Status": label if not render.active else "Rendering",
        "Size": size,
        "Length (s)": round(render.duration_s, 1) if render.duration_s else None,
        "Audio": "yes" if render.has_audio else ("no" if render.has_audio is False else "-"),
        "File size": human_size(render.size_bytes) if render.size_bytes else "-",
        "File": str(render.output_path) if render.output_path else "",
        "Note": render.error or "",
    }


def production_rows(job: Job) -> list[dict[str, Any]]:
    """Per-clip rows of a ``produce_video`` job result (empty when it has none)."""
    clips = (job.result or {}).get("clips")
    if not isinstance(clips, list):
        return []
    rows: list[dict[str, Any]] = []
    for clip in clips:
        label, _ = clip_status(str(clip.get("status", "")))
        rows.append(
            {
                "#": clip.get("rank"),
                "Title": romanize(str(clip.get("title") or "")),
                "Score": clip.get("score"),
                "Status": label,
                "Length (s)": clip.get("duration_s"),
                "Size": (f"{clip.get('width')}x{clip.get('height')}" if clip.get("width") else "-"),
                "Output": clip.get("output") or "",
                "Reason": clip.get("error") or ("reused" if clip.get("reused") else ""),
            }
        )
    return rows
