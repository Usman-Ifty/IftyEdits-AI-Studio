"""Overview: configuration, library size and worker status."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import ALL_STAGES, StageStatus
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.storage.db import open_database
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui.formatting import human_size

settings = Settings.from_env()

st.title("IftyEdits AI Studio")
st.caption("Local-first Shorts studio. Originals are stored read-only and never modified.")

with closing(open_database(settings.db_path)) as conn:
    videos = VideoRepository(conn).list_recent(limit=100_000)
    queue = JobQueue(conn)
    workers = queue.live_workers()
    active = queue.count_active()
    summary = ProcessingRepository(conn).stage_summary()
    transcribed = len(TranscriptRepository(conn).current_by_video())

done = (StageStatus.SUCCEEDED, StageStatus.SKIPPED)
prepared = sum(1 for v in videos if all(summary.get(v.id, {}).get(s) in done for s in ALL_STAGES))
failed = sum(1 for v in videos if StageStatus.FAILED in summary.get(v.id, {}).values())

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Videos", len(videos))
col2.metric("Library size", human_size(sum(v.size_bytes for v in videos)))
col3.metric("Media prepared", f"{prepared} / {len(videos)}")
col4.metric("Transcribed", f"{transcribed} / {len(videos)}")
col5.metric("Active jobs", active)
if failed:
    st.error(f"{failed} video(s) have a failed preparation stage. See the Library page.")
elif videos and prepared < len(videos):
    st.info(
        f"{len(videos) - prepared} video(s) still need media preparation "
        "(Library page: Prepare media, or `uv run iftyedits process --all`)."
    )

if workers:
    st.success(f"Worker running: {', '.join(workers)}")
else:
    st.warning(
        "No background worker is running, so queued imports will wait. "
        "Start one with `uv run iftyedits worker`, or launch the app with `uv run iftyedits ui`."
    )

st.subheader("Storage")
st.code(
    f"data      {settings.data_dir}\n"
    f"originals {settings.originals_dir}  (read-only vault)\n"
    f"derived   {settings.derived_dir}  (audio, proxies, thumbnails)\n"
    f"exports   {settings.exports_dir}  (saved transcript exports)\n"
    f"models    {settings.models_dir}  (speech models, downloaded on request)\n"
    f"database  {settings.db_path}  (transcripts are stored here)",
    language="text",
)
