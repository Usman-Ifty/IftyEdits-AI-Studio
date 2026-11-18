"""Library page: imported videos and their technical metadata."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.storage.db import open_database
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui.formatting import human_duration, human_size, stream_row, video_row

settings = Settings.from_env()

st.title("Library")

with closing(open_database(settings.db_path)) as conn:
    repo = VideoRepository(conn)
    videos = repo.list_recent()
    streams = {v.id: repo.streams(v.id) for v in videos}
    sources = {v.id: repo.sources(v.id) for v in videos}

if not videos:
    st.info("No videos imported yet. Use the Import page to add some.")
    st.stop()

st.dataframe(
    [video_row(v, streams[v.id]) for v in videos],
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

st.markdown("**Streams**")
st.dataframe([stream_row(s) for s in streams[video.id]], hide_index=True, width="stretch")

st.markdown("**Integrity**")
st.code(
    f"SHA-256  {video.sha256}\nvault    {settings.originals_dir / video.vault_relpath}",
    language="text",
)

st.markdown("**Imported from**")
if sources[video.id]:
    for source in sources[video.id]:
        st.text(f"{source.source_path}   ({source.imported_at[:19].replace('T', ' ')} UTC)")
else:
    st.caption("Recovered by reconcile; original location unknown.")
