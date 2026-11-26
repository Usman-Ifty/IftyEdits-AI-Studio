"""Turn domain objects into display rows and human-readable strings."""

from __future__ import annotations

from typing import Any

from iftyedits.core.clip_models import ClipAnalysis
from iftyedits.core.languages import language_name
from iftyedits.core.models import (
    ALL_STAGES,
    SEARCH_MATCH_END,
    SEARCH_MATCH_START,
    Artifact,
    ArtifactKind,
    Job,
    JobStatus,
    ModelState,
    ModelStatus,
    Stage,
    StageRecord,
    StageStatus,
    StoredSegment,
    StreamInfo,
    Transcript,
    TranscriptSearchHit,
    Video,
)

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


def utc_short(timestamp: str | None) -> str:
    """ISO timestamp as ``YYYY-MM-DD HH:MM:SS``; empty when missing."""
    return timestamp[:19].replace("T", " ") if timestamp else ""


def job_target(job: Job) -> str:
    """What a job works on: a file name for imports, a video id for processing."""
    if "path" in job.payload:
        return str(job.payload["path"]).rsplit("/", 1)[-1] or "-"
    if "url" in job.payload:
        return str(job.payload["url"]).removeprefix("https://www.")
    if job.video_id is not None:
        return f"video #{job.video_id}"
    if "model" in job.payload:
        return f"model {job.payload['model']}"
    return "-"


def job_status_label(job: Job) -> str:
    """Status, noting a pending cancellation of a running job."""
    if job.status is JobStatus.RUNNING and job.cancel_requested_at:
        return "cancelling"
    return job.status.value


def job_outcome(job: Job) -> str:
    """Short result: import outcome, or how many processing stages succeeded."""
    if not job.result:
        return ""
    if "outcome" in job.result:
        return str(job.result["outcome"])
    if "analysis_id" in job.result:
        count = job.result.get("candidates", 0)
        return "reused" if job.result.get("reused") else f"{count} suggestions"
    if "transcript_id" in job.result:
        segments = job.result.get("segments", 0)
        return "reused" if job.result.get("reused") else f"{segments} segments"
    if "state" in job.result:
        return str(job.result["state"])
    if "clips" in job.result:
        done, total = job.result.get("succeeded", 0), len(job.result["clips"])
        return f"{done}/{total} Shorts"
    stages = job.result.get("stages")
    if isinstance(stages, dict) and stages:
        done = sum(1 for s in stages.values() if s in ("succeeded", "skipped"))
        return f"{done}/{len(stages)} stages"
    return ""


def job_row(job: Job) -> dict[str, Any]:
    """One job table row."""
    return {
        "ID": job.id,
        "Kind": JOB_KIND_LABELS.get(job.kind, job.kind),
        "File": job_target(job),
        "Status": job_status_label(job),
        "Outcome": job_outcome(job),
        "Progress": job.progress,
        "Detail": job.error or job.message or "",
        "Attempts": job.attempts,
        "Created (UTC)": utc_short(job.created_at),
    }


JOB_KIND_LABELS = {
    "import_file": "import",
    "process_video": "prepare media",
    "transcribe_video": "transcribe",
    "analyze_clips": "analyse clips",
    "render_short": "render Short",
    "download_model": "download model",
    "fetch_url": "download link",
    "produce_video": "produce Shorts",
}

STAGE_LABELS = {Stage.AUDIO: "Audio", Stage.PROXY: "Proxy", Stage.THUMBNAIL: "Thumbnail"}
STAGE_STATUS_LABELS = {
    StageStatus.RUNNING: "⏳ running",
    StageStatus.SUCCEEDED: "✅ done",
    StageStatus.FAILED: "❌ failed",
    StageStatus.SKIPPED: "⚪ not needed",
    StageStatus.CANCELLED: "⏹ cancelled",
}
NOT_STARTED = "· not started"


def stage_cell(status: StageStatus | None) -> str:
    """Compact stage status for tables."""
    return STAGE_STATUS_LABELS[status] if status is not None else NOT_STARTED


def processing_columns(statuses: dict[Stage, StageStatus]) -> dict[str, str]:
    """Library table columns: one per stage."""
    return {STAGE_LABELS[s]: stage_cell(statuses.get(s)) for s in ALL_STAGES}


def stage_row(stage: StageRecord) -> dict[str, Any]:
    """One processing stage row."""
    return {
        "Stage": STAGE_LABELS[stage.stage],
        "Status": stage_cell(stage.status),
        "Progress": stage.progress,
        "Started (UTC)": utc_short(stage.started_at),
        "Finished (UTC)": utc_short(stage.finished_at),
        "Detail": stage.error or stage.message or "",
    }


def artifact_details(artifact: Artifact) -> str:
    """Human summary of an artifact's recorded metadata."""
    meta = artifact.metadata
    if artifact.kind is ArtifactKind.AUDIO_WAV:
        return f"{meta.get('sample_rate')} Hz, {meta.get('channels')} ch, " + human_duration(
            meta.get("duration_s")
        )
    if artifact.kind is ArtifactKind.PROXY_MP4:
        engine = "VideoToolbox (hardware)" if meta.get("hardware_encoded") else meta.get("encoder")
        audio = "with audio" if meta.get("has_audio") else "no audio"
        return (
            f"{meta.get('width')}x{meta.get('height')}, {meta.get('fps')} fps, {audio}, "
            f"{engine}, {human_duration(meta.get('duration_s'))}"
        )
    return f"{meta.get('width')}x{meta.get('height')} JPEG from {meta.get('input')}"


def artifact_row(artifact: Artifact) -> dict[str, Any]:
    """One generated-file row."""
    return {
        "Kind": artifact.kind.value,
        "Details": artifact_details(artifact),
        "Size": human_size(artifact.size_bytes),
        "SHA-256": artifact.sha256[:12],
        "Job": artifact.job_id if artifact.job_id is not None else "-",
        "Created (UTC)": utc_short(artifact.created_at),
        "Path": artifact.relpath,
    }


# ---------------------------------------------------------------------- transcripts


def timestamp(seconds: float) -> str:
    """``M:SS.mmm`` or ``H:MM:SS.mmm`` for transcript tables."""
    total_ms = round(max(0.0, seconds) * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes}:{secs:02d}.{millis:03d}"


def transcript_cell(transcript: Transcript | None, job: Job | None) -> str:
    """Library column: state of a video's transcription."""
    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return "⏳ running" if job.status is JobStatus.RUNNING else "⏳ queued"
    if transcript is not None:
        return "✅ no speech" if transcript.is_empty else "✅ done"
    if job is not None and job.status is JobStatus.FAILED:
        return "❌ failed"
    if job is not None and job.status is JobStatus.CANCELLED:
        return "⏹ cancelled"
    return NOT_STARTED


def clips_cell(analysis: ClipAnalysis | None, job: Job | None) -> str:
    """Library column: state of a video's clip analysis."""
    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return "⏳ running" if job.status is JobStatus.RUNNING else "⏳ queued"
    if analysis is not None:
        return f"✅ {analysis.candidate_count} suggestions"
    if job is not None and job.status is JobStatus.FAILED:
        return "❌ failed"
    if job is not None and job.status is JobStatus.CANCELLED:
        return "⏹ cancelled"
    return NOT_STARTED


def language_label(code: str | None, probability: float | None = None) -> str:
    """``English (en, 98%)``; ``Unknown`` when no language was detected."""
    if code is None:
        return "Unknown"
    label = f"{language_name(code)} ({code}"
    if probability is not None:
        label += f", {probability:.0%}"
    return label + ")"


def transcript_facts(transcript: Transcript) -> dict[str, str]:
    """Headline facts about a transcript, for metrics and the CLI."""
    speed = ""
    if transcript.processing_s and transcript.duration_s:
        speed = f"{transcript.duration_s / transcript.processing_s:.1f}x real time"
    return {
        "Model": f"{transcript.model} ({transcript.compute_type}, {transcript.device})",
        "Language": language_label(transcript.language, transcript.language_probability),
        "Segments": str(transcript.segment_count),
        "Words": str(transcript.word_count),
        "Audio": human_duration(transcript.duration_s),
        "Processing": (
            f"{transcript.processing_s:.1f}s" if transcript.processing_s is not None else "-"
        )
        + (f" ({speed})" if speed else ""),
        "Created (UTC)": utc_short(transcript.created_at),
    }


def segment_row(segment: StoredSegment) -> dict[str, Any]:
    """One transcript table row."""
    return {
        "#": segment.position + 1,
        "Start": timestamp(segment.start_s),
        "End": timestamp(segment.end_s),
        "Text": segment.text,
        "Words": len(segment.words),
    }


def word_row(
    index: int, start_s: float, end_s: float, text: str, prob: float | None
) -> dict[str, Any]:
    """One word-timing row."""
    return {
        "#": index + 1,
        "Start": timestamp(start_s),
        "End": timestamp(end_s),
        "Word": text,
        "Confidence": f"{prob:.0%}" if prob is not None else "-",
    }


MODEL_STATE_LABELS = {
    ModelState.INSTALLED: "✅ installed",
    ModelState.MISSING: "· not downloaded",
    ModelState.INCOMPLETE: "⚠️ incomplete",
}


def model_row(status: ModelStatus, downloading: Job | None = None) -> dict[str, Any]:
    """One speech-model table row."""
    state = MODEL_STATE_LABELS[status.state]
    if downloading is not None:
        state = f"⏳ downloading {downloading.progress:.0%}"
    return {
        "Model": status.spec.name,
        "Status": state,
        "Download": human_size(status.spec.size_bytes),
        "RAM (approx.)": f"{status.spec.ram_gb:.1f} GB",
        "Notes": status.spec.summary,
    }


_MARKDOWN_SPECIAL = "\\`*_{}[]()#+-.!|<>~$"


def escape_markdown(text: str) -> str:
    """Escape characters Streamlit markdown would interpret."""
    return "".join(f"\\{c}" if c in _MARKDOWN_SPECIAL else c for c in text)


def highlight_snippet(snippet: str) -> str:
    """Search snippet as markdown with matches in bold; the text itself is escaped."""
    escaped = escape_markdown(snippet)
    return escaped.replace(SEARCH_MATCH_START, "**").replace(SEARCH_MATCH_END, "**")


def search_row(hit: TranscriptSearchHit) -> dict[str, Any]:
    """One search result row (plain text, matches not marked)."""
    plain = hit.snippet.replace(SEARCH_MATCH_START, "").replace(SEARCH_MATCH_END, "")
    return {
        "Video": f"#{hit.video_id} {hit.original_filename}",
        "Start": timestamp(hit.start_s),
        "End": timestamp(hit.end_s),
        "Match": plain,
    }
