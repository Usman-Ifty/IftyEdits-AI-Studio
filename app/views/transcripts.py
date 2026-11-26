"""Transcripts page: speech models, transcription jobs, transcript viewer, exports, search."""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import streamlit as st

from iftyedits.adapters.model_store import build_model_store
from iftyedits.config import Settings
from iftyedits.core.languages import WHISPER_LANGUAGES, language_name
from iftyedits.core.models import CancelOutcome, Job, JobStatus, ModelState, Video
from iftyedits.pipeline.jobs import DOWNLOAD_MODEL, TRANSCRIBE_VIDEO, JobQueue
from iftyedits.services.subtitles import export
from iftyedits.services.transcription import transcription_blocker
from iftyedits.storage.db import open_database
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome
from iftyedits.ui.formatting import (
    highlight_snippet,
    human_size,
    job_status_label,
    language_label,
    model_row,
    segment_row,
    timestamp,
    transcript_facts,
    utc_short,
    word_row,
)

settings = Settings.from_env()
store = build_model_store(settings)
AUTO = "Auto-detect"
LANGUAGE_CHOICES = [AUTO, *sorted(f"{name} ({code})" for code, name in WHISPER_LANGUAGES.items())]
UNCERTAIN_LANGUAGE = 0.6

chrome.heading("Transcripts")
st.caption(
    "Speech-to-text runs on this Mac in the background worker with faster-whisper. "
    "Audio never leaves the computer. Models are downloaded only when you ask."
)


def _language_code(choice: str) -> str:
    return "auto" if choice == AUTO else choice.rsplit("(", 1)[1].rstrip(")")


def _worker_warning(workers: list[str], job: Job) -> None:
    if not workers and job.status is JobStatus.QUEUED:
        st.warning("No worker is running. Start `uv run iftyedits worker`.")


def _cancel_button(job: Job, label: str) -> None:
    if job.cancel_requested_at:
        st.caption("Cancellation requested; waiting for the worker to stop.")
        return
    if st.button(label, key=f"cancel-{job.id}"):
        with closing(open_database(settings.db_path)) as conn:
            outcome = JobQueue(conn).request_cancel(job.id)
        text = {
            CancelOutcome.CANCELLED: "Cancelled before it started.",
            CancelOutcome.REQUESTED: "Cancellation requested.",
        }.get(outcome, "The job had already finished.")
        st.success(text)


# ---------------------------------------------------------------------- feedback


def _flash(area: str, kind: str, text: str) -> None:
    """Show a message in ``area`` after the rerun that follows an action."""
    st.session_state.setdefault(f"flash-{area}", []).append((kind, text))


def _show_flash(area: str) -> None:
    for kind, text in st.session_state.pop(f"flash-{area}", []):
        (st.success if kind == "success" else st.info)(text)


def _rerun_when_finished(key: str, active_ids: set[int]) -> None:
    """Re-render the whole page once jobs this fragment saw running have finished."""
    seen: set[int] = st.session_state.get(key, set())
    st.session_state[key] = active_ids
    if seen - active_ids:
        st.rerun()


# ---------------------------------------------------------------------- models


@st.fragment(run_every=2)
def download_status() -> None:
    """Model table and download progress; refreshed every two seconds."""
    statuses = store.statuses()
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        downloads = {str(j.payload.get("model")): j for j in queue.active_downloads()}
        recent = [j for j in queue.list_recent(30) if j.kind == DOWNLOAD_MODEL]
        workers = queue.live_workers()
    _rerun_when_finished("seen-downloads", {j.id for j in downloads.values()})

    st.dataframe(
        [model_row(s, downloads.get(s.spec.name)) for s in statuses],
        hide_index=True,
        width="stretch",
    )
    for name, job in downloads.items():
        st.progress(min(1.0, max(0.0, job.progress)), text=job.message or f"Waiting: {name}")
        _worker_warning(workers, job)
        _cancel_button(job, f"Cancel {name} download")
    failed = [j for j in recent if j.status is JobStatus.FAILED][:1]
    for job in failed:
        if store.status(str(job.payload.get("model"))).state is not ModelState.INSTALLED:
            st.error(f"Last download of {job.payload.get('model')} failed: {job.error}")


def download_controls() -> None:
    """Choose a model and queue its download (outside the refreshing fragment)."""
    statuses = store.statuses()
    missing = [s.spec.name for s in statuses if s.state is not ModelState.INSTALLED]
    if not missing:
        st.success("Every supported model is installed.")
        return
    with closing(open_database(settings.db_path)) as conn:
        downloading = {str(j.payload.get("model")) for j in JobQueue(conn).active_downloads()}
    default = (
        missing.index(settings.transcription_model)
        if settings.transcription_model in missing
        else 0
    )
    choice = st.selectbox("Model to download", missing, index=default, key="download-choice")
    spec = store.spec(choice)
    store.root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(store.root).free
    st.caption(
        f"{spec.name}: {human_size(spec.size_bytes)} download from huggingface.co/{spec.repo_id} "
        f"(pinned revision {spec.revision[:12]}), about {spec.ram_gb:.1f} GB RAM while "
        f"transcribing. Free disk space: {human_size(free)}. Saved in {store.root}."
    )
    if st.button(
        f"Download {spec.name} ({human_size(spec.size_bytes)})",
        key="download-model",
        disabled=choice in downloading,
    ):
        with closing(open_database(settings.db_path)) as conn:
            job_id, created = JobQueue(conn).enqueue_model_download(spec.name)
        if created:
            _flash("models", "success", f"Queued download job {job_id}.")
        else:
            _flash("models", "info", f"Job {job_id} is already downloading {spec.name}.")
        st.rerun()


installed_models = [s.spec.name for s in store.statuses() if s.state is ModelState.INSTALLED]
with st.expander("Speech models", expanded=not installed_models):
    download_status()
    _show_flash("models")
    download_controls()

# ---------------------------------------------------------------------- videos

with closing(open_database(settings.db_path)) as conn:
    video_repo = VideoRepository(conn)
    videos = video_repo.list_recent()
    has_audio = {
        v.id: any(s.codec_type == "audio" for s in video_repo.streams(v.id)) for v in videos
    }

if not videos:
    st.info("No videos imported yet. Open Create Shorts to add some.")
    chrome.halt()

labels = {f"#{v.id}  {v.original_filename}": v for v in videos}
video = labels[st.selectbox("Video", list(labels), key="transcript-video")]


@st.fragment(run_every=2)
def transcription_status(video: Video) -> None:
    """Progress, cancellation and last failure of this video's transcription."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        active = queue.active_transcription(video.id)
        latest = queue.latest(TRANSCRIBE_VIDEO, video.id)
        workers = queue.live_workers()
    _rerun_when_finished(f"seen-transcription-{video.id}", {active.id} if active else set())

    if active is not None:
        st.info(
            f"Job {active.id} is {job_status_label(active)}: "
            f"{active.message or 'waiting for a worker'} ({active.progress:.0%})"
        )
        st.progress(min(1.0, max(0.0, active.progress)))
        _worker_warning(workers, active)
        _cancel_button(active, "Cancel transcription")
    elif latest is not None and latest.status is JobStatus.FAILED:
        st.error(
            f"Transcription job {latest.id} failed ({utc_short(latest.finished_at)} UTC): "
            f"{latest.error}"
        )
        st.caption("Fix the cause above, then press Transcribe to retry.")
    elif latest is not None and latest.status is JobStatus.CANCELLED:
        st.caption(f"Transcription job {latest.id} was cancelled. Press Transcribe to start again.")


def transcription_controls(video: Video) -> None:
    """Model, language and the Transcribe button (outside the refreshing fragment)."""
    with closing(open_database(settings.db_path)) as conn:
        active = JobQueue(conn).active_transcription(video.id)
    installed = [s.spec.name for s in store.statuses() if s.state is ModelState.INSTALLED]
    if not installed:
        st.warning("No speech model is installed yet. Download one under Speech models above.")
        return
    left, mid, right = st.columns([1, 1, 1])
    default = (
        installed.index(settings.transcription_model)
        if settings.transcription_model in installed
        else 0
    )
    model = left.selectbox("Model", installed, index=default, key=f"model-{video.id}")
    default_language = settings.transcription_language
    language_index = 0
    if default_language != "auto" and default_language in WHISPER_LANGUAGES:
        language_index = LANGUAGE_CHOICES.index(
            f"{WHISPER_LANGUAGES[default_language]} ({default_language})"
        )
    language = mid.selectbox(
        "Language", LANGUAGE_CHOICES, index=language_index, key=f"language-{video.id}"
    )
    force = right.checkbox(
        "Transcribe again",
        key=f"force-{video.id}",
        help="Run the model even if a transcript with the same settings exists.",
    )
    if st.button(
        "Transcribe",
        key=f"transcribe-{video.id}",
        type="primary",
        disabled=active is not None,
        help="Uses the prepared 16 kHz audio (prepared automatically if missing).",
    ):
        code = _language_code(language)
        with closing(open_database(settings.db_path)) as conn:
            job_id, created = JobQueue(conn).enqueue_transcription(
                video.id, model=model, language=None if code == "auto" else code, force=force
            )
        if created:
            _flash("transcribe", "success", f"Queued transcription job {job_id}.")
        else:
            _flash("transcribe", "info", f"Job {job_id} is already transcribing this video.")
        st.rerun()


st.subheader("Transcribe")
_show_flash("transcribe")
blocker = transcription_blocker(video, has_audio[video.id])
if blocker is not None:
    st.warning(blocker + ".")
else:
    transcription_status(video)
    transcription_controls(video)

# ---------------------------------------------------------------------- viewer

st.subheader("Transcript")
with closing(open_database(settings.db_path)) as conn:
    transcript_repo = TranscriptRepository(conn)
    versions = transcript_repo.for_video(video.id)

if not versions:
    st.caption("No transcript yet.")
else:
    version_labels = {
        f"{'★ ' if t.is_current else ''}{t.model}, {language_label(t.language)}, "
        f"{utc_short(t.created_at)} UTC (#{t.id})": t
        for t in versions
    }
    current_label = (
        next(k for k, t in version_labels.items() if t.is_current)
        if any(t.is_current for t in versions)
        else next(iter(version_labels))
    )
    chosen = version_labels[
        st.selectbox(
            "Version",
            list(version_labels),
            index=list(version_labels).index(current_label),
            key=f"version-{video.id}",
            help="★ marks the transcript later steps (clip finding, captions) will use.",
        )
    ]
    if not chosen.is_current and st.button("Use this version", key=f"use-{chosen.id}"):
        with closing(open_database(settings.db_path)) as conn:
            TranscriptRepository(conn).make_current(chosen.id)
        st.rerun()

    facts = transcript_facts(chosen)
    cols = st.columns(4)
    cols[0].metric("Model", chosen.model)
    cols[1].metric("Language", language_name(chosen.language))
    cols[2].metric("Segments", facts["Segments"])
    cols[3].metric("Words", facts["Words"])
    st.caption(
        f"{facts['Model']} · language {facts['Language']} · audio {facts['Audio']} · "
        f"processing {facts['Processing']} · created {facts['Created (UTC)']} UTC"
    )
    if (
        chosen.language_requested is None
        and chosen.language_probability is not None
        and chosen.language_probability < UNCERTAIN_LANGUAGE
        and not chosen.is_empty
    ):
        st.warning(
            f"Language detection was uncertain ({chosen.language_probability:.0%}). If the "
            "text looks wrong, choose the language explicitly and transcribe again."
        )
    if chosen.dropped_segments:
        st.caption(f"{chosen.dropped_segments} segment(s) with invalid timings were discarded.")

    with closing(open_database(settings.db_path)) as conn:
        segments = TranscriptRepository(conn).segments(chosen.id)

    if chosen.is_empty:
        st.info(
            "No speech was detected in this video's audio. If it does contain speech, try "
            "a larger model or set the language explicitly."
        )
    else:
        st.dataframe([segment_row(s) for s in segments], hide_index=True, width="stretch")
        numbers = [s.position + 1 for s in segments if s.words]
        if numbers:
            with st.expander("Word timings"):
                number = st.selectbox("Segment", numbers, key=f"words-{chosen.id}")
                segment = segments[number - 1]
                st.caption(
                    f"{timestamp(segment.start_s)} to {timestamp(segment.end_s)}: {segment.text}"
                )
                st.dataframe(
                    [
                        word_row(i, w.start_s, w.end_s, w.text, w.probability)
                        for i, w in enumerate(segment.words)
                    ],
                    hide_index=True,
                    width="stretch",
                )

    stem = f"{Path(video.original_filename).stem}-{chosen.model}"
    mime = {"srt": "application/x-subrip", "vtt": "text/vtt", "txt": "text/plain"}
    download_cols = st.columns(4)
    for col, fmt in zip(download_cols, ("srt", "vtt", "txt", "json"), strict=True):
        col.download_button(
            f"Download {fmt.upper()}",
            data=export(fmt, chosen, segments),
            file_name=f"{stem}.{fmt}",
            mime=mime.get(fmt, "application/json"),
            key=f"download-{fmt}-{chosen.id}",
        )

# ---------------------------------------------------------------------- search

st.subheader("Search")
query = st.text_input("Find words in transcripts", key="search-query", placeholder="e.g. money")
scope = st.radio("Search in", ["This video", "All videos"], horizontal=True, key="search-scope")
if query.strip():
    with closing(open_database(settings.db_path)) as conn:
        hits = TranscriptRepository(conn).search(
            query, video_id=video.id if scope == "This video" else None, limit=50
        )
    if not hits:
        st.caption("No matches in the current transcripts.")
    for hit in hits:
        st.markdown(
            f"`{timestamp(hit.start_s)}` **#{hit.video_id}** {highlight_snippet(hit.snippet)}"
        )
