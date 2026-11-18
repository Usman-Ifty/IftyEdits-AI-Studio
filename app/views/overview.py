"""Overview: configuration, library size and worker status."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.storage.db import open_database
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

col1, col2, col3 = st.columns(3)
col1.metric("Videos", len(videos))
col2.metric("Library size", human_size(sum(v.size_bytes for v in videos)))
col3.metric("Active jobs", active)

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
    f"database  {settings.db_path}",
    language="text",
)
