"""Library page: imported videos, their metadata and media preparation."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import ALL_STAGES, ArtifactKind, CancelOutcome, StageStatus, Video
from iftyedits.pipeline.jobs import ANALYZE_CLIPS, TRANSCRIBE_VIDEO, JobQueue
from iftyedits.services.production import ProductionOptions
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.originals import is_within
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome
from iftyedits.ui.formatting import (
    artifact_row,
    clips_cell,
    human_duration,
    human_size,
    job_status_label,
    processing_columns,
    stage_row,
    stream_row,
    transcript_cell,
    video_row,
)

settings = Settings.from_env()
DONE = (StageStatus.SUCCEEDED, StageStatus.SKIPPED)

chrome.heading("Library")

with closing(open_database(settings.db_path)) as conn:
    repo = VideoRepository(conn)
    videos = repo.list_recent()
    streams = {v.id: repo.streams(v.id) for v in videos}
    sources = {v.id: repo.sources(v.id) for v in videos}
    summary = ProcessingRepository(conn).stage_summary()
    transcripts = TranscriptRepository(conn).current_by_video()
    transcription_jobs = JobQueue(conn).latest_by_video(TRANSCRIBE_VIDEO)
    clip_analyses = ClipRepository(conn).current_by_video()
    clip_jobs = JobQueue(conn).latest_by_video(ANALYZE_CLIPS)

if not videos:
    st.info(
        "No videos yet. Open **Create Shorts** to paste a YouTube link or add a local file, "
        "or **Guides & Help** for a short walkthrough."
    )
    chrome.halt()

st.dataframe(
    [
        {
            **video_row(v, streams[v.id], len(sources[v.id])),
            **processing_columns(summary.get(v.id, {})),
            "Transcript": transcript_cell(transcripts.get(v.id), transcription_jobs.get(v.id)),
            "Clips": clips_cell(clip_analyses.get(v.id), clip_jobs.get(v.id)),
        }
        for v in videos
    ],
    hide_index=True,
    width="stretch",
)

labels = {f"#{v.id}  {v.original_filename}": v for v in videos}
choice = st.selectbox("Details", list(labels))
video = labels[choice]

st.subheader(video.original_filename)
col1, col2, col3, col4 = st.columns(4)
col1.metric("Duration", human_duration(video.duration_s))
col2.metric("Size", human_size(video.size_bytes))
col3.metric("Container", (video.format_name or "-").split(",")[0])
col4.metric("Status", video.status.value)
if video.probe_error:
    st.error(f"Probe failed: {video.probe_error}")
current_transcript = transcripts.get(video.id)
if current_transcript is not None:
    st.caption(
        f"Transcript: {current_transcript.segment_count} segments with {current_transcript.model}. "
        "Open My Videos > Transcripts to read, search or export it."
    )
else:
    st.caption("No transcript yet. Open My Videos > Transcripts to transcribe this video.")


@st.fragment(run_every=2)
def media_panel(video: Video) -> None:
    """Thumbnail, stage status, actions and generated files; refreshed every two seconds."""
    with closing(open_database(settings.db_path)) as conn:
        processing = ProcessingRepository(conn)
        stages = processing.stages(video.id)
        artifacts = processing.artifacts(video.id)
        active = JobQueue(conn).active_processing(video.id)
        workers = JobQueue(conn).live_workers()

    st.markdown("**Media preparation**")
    thumb = next((a for a in artifacts if a.kind is ArtifactKind.THUMBNAIL_JPG), None)
    left, right = st.columns([1, 2])
    with left:
        thumb_path = settings.derived_dir / thumb.relpath if thumb else None
        if (
            thumb_path is not None
            and is_within(thumb_path, settings.derived_dir)
            and thumb_path.is_file()
        ):
            st.image(str(thumb_path), caption="Thumbnail", width="stretch")
        else:
            st.caption("No thumbnail yet. Prepare media to generate one.")
    with right:
        statuses = {s.stage: s.status for s in stages}
        remaining = [s.value for s in ALL_STAGES if statuses.get(s) not in DONE]
        if active is not None:
            state = job_status_label(active)
            st.info(
                f"Job {active.id} is {state}: {active.message or 'waiting for a worker'} "
                f"({active.progress:.0%})"
            )
            st.progress(min(1.0, max(0.0, active.progress)))
            if not workers and active.status.value == "queued":
                st.warning("No worker is running. Start `uv run iftyedits worker`.")
            if active.cancel_requested_at:
                st.caption("Cancellation requested; waiting for the worker to stop FFmpeg.")
            elif st.button("Cancel processing", key=f"cancel-{active.id}"):
                with closing(open_database(settings.db_path)) as conn:
                    outcome = JobQueue(conn).request_cancel(active.id)
                if outcome is CancelOutcome.CANCELLED:
                    st.success("Cancelled before it started.")
                elif outcome is CancelOutcome.REQUESTED:
                    st.success("Cancellation requested.")
                else:
                    st.info("The job had already finished.")
        elif remaining:
            st.caption("Still to do: " + ", ".join(remaining))
        else:
            st.success("Audio, proxy and thumbnail are ready.")
        b1, b2 = st.columns(2)
        prepare = b1.button(
            "Prepare media",
            key=f"prepare-{video.id}",
            disabled=active is not None,
            help="Extract 16 kHz audio, make a 720p proxy and a thumbnail. "
            "Valid existing outputs are reused.",
            type="primary",
        )
        rebuild = b2.button(
            "Regenerate all",
            key=f"rebuild-{video.id}",
            disabled=active is not None or not stages,
            help="Run every stage again even if its output is valid.",
        )
        produce = st.button(
            "Produce Shorts",
            key=f"produce-{video.id}",
            disabled=active is not None,
            help="Prepare, transcribe, analyze and export the best clips as separate "
            "1080x1920 MP4 files, in one job. Finished steps are reused.",
        )
        if produce:
            options = ProductionOptions()
            with closing(open_database(settings.db_path)) as conn:
                job_id, created = JobQueue(conn).enqueue_production(
                    options.as_payload(), video_id=video.id
                )
            st.success(f"Queued production job {job_id}. Follow it on the Processing Queue page.")
        if prepare or rebuild:
            with closing(open_database(settings.db_path)) as conn:
                job_id, created = JobQueue(conn).enqueue_processing(
                    video.id, ALL_STAGES, force=rebuild
                )
            if created:
                st.success(f"Queued job {job_id}.")
            else:
                st.info(f"Job {job_id} is already queued for this video.")

    if stages:
        st.dataframe(
            [stage_row(s) for s in stages],
            hide_index=True,
            width="stretch",
            column_config={
                "Progress": st.column_config.ProgressColumn(
                    min_value=0.0, max_value=1.0, format="percent"
                )
            },
        )
        for stage in stages:
            if stage.status is StageStatus.FAILED and stage.error:
                st.error(f"{stage.stage.value}: {stage.error}")
    else:
        st.caption("This video has not been prepared yet.")

    with st.expander("Generated files"):
        if artifacts:
            st.dataframe([artifact_row(a) for a in artifacts], hide_index=True, width="stretch")
            st.caption(f"Stored under {settings.derived_dir}")
        else:
            st.caption("None yet.")


media_panel(video)

with st.expander("Technical details (streams, integrity, source)"):
    st.markdown("**Streams**")
    st.dataframe([stream_row(s) for s in streams[video.id]], hide_index=True, width="stretch")

    st.markdown("**Integrity**")
    st.code(
        f"SHA-256  {video.sha256}\nvault    {settings.originals_dir / video.vault_relpath}",
        language="text",
        wrap_lines=True,
    )

    st.markdown("**Imported from**")
    if len(sources[video.id]) > 1:
        st.info(
            f"Identical content was imported from {len(sources[video.id])} locations. "
            "It is stored once in the vault."
        )
    if sources[video.id]:
        for source in sources[video.id]:
            st.text(f"{source.source_path}   ({source.imported_at[:19].replace('T', ' ')} UTC)")
    else:
        st.caption("Recovered by reconcile; original location unknown.")
