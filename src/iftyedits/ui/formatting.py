"""Turn domain objects into display rows and human-readable strings."""

from __future__ import annotations

from typing import Any

from iftyedits.core.models import Job, StreamInfo, Video

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(size: int) -> str:
    """Bytes as a short string using 1024-based units, e.g. ``1.5 GB``."""
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def human_duration(seconds: float | None) -> str:
    """Seconds as ``H:MM:SS`` or ``M:SS``; ``-`` when unknown."""
    if seconds is None:
        return "-"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def resolution(streams: list[StreamInfo]) -> str:
    """Display resolution of the first real video stream, after rotation."""
    for stream in streams:
        if stream.codec_type == "video" and not stream.is_attached_pic:
            size = stream.display_size
            if size:
                return f"{size[0]}x{size[1]}"
    return "-"


def video_row(video: Video, streams: list[StreamInfo], source_count: int) -> dict[str, Any]:
    """One library table row. ``source_count`` > 1 means duplicate imports were merged."""
    return {
        "ID": video.id,
        "File": video.original_filename,
        "Duration": human_duration(video.duration_s),
        "Resolution": resolution(streams),
        "Size": human_size(video.size_bytes),
        "Status": video.status.value,
        "Sources": source_count,
        "Imported (UTC)": video.imported_at[:19].replace("T", " "),
        "SHA-256": video.sha256[:12],
    }


def stream_row(stream: StreamInfo) -> dict[str, Any]:
    """One stream details row."""
    fps = stream.avg_frame_rate
    flags = [
        name
        for name, on in (
            ("HDR", stream.is_hdr),
            ("VFR", stream.is_vfr),
            ("cover art", stream.is_attached_pic),
        )
        if on
    ]
    return {
        "#": stream.index,
        "Type": stream.codec_type,
        "Codec": stream.codec_name or "-",
        "Profile": stream.profile or "-",
        "Size": f"{stream.width}x{stream.height}" if stream.width and stream.height else "-",
        "FPS": f"{fps:.3f}" if fps else "-",
        "Rotation": stream.rotation,
        "Audio": (
            f"{stream.sample_rate} Hz, {stream.channels} ch"
            if stream.codec_type == "audio" and stream.sample_rate
            else "-"
        ),
        "Language": stream.language or "-",
        "Flags": ", ".join(flags) or "-",
    }


def job_row(job: Job) -> dict[str, Any]:
    """One job table row."""
    target = str(job.payload.get("path", "")).rsplit("/", 1)[-1]
    return {
        "ID": job.id,
        "Kind": job.kind,
        "File": target or "-",
        "Status": job.status.value,
        "Outcome": str(job.result.get("outcome", "")) if job.result else "",
        "Progress": job.progress,
        "Detail": job.error or job.message or "",
        "Attempts": job.attempts,
        "Created (UTC)": job.created_at[:19].replace("T", " "),
    }
