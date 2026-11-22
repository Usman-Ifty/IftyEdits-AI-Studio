"""Exports: every finished Short, grouped by source video, plus production summaries."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import KIND_EXPORT, ShortRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import theme
from iftyedits.ui.exports import export_row, production_rows
from iftyedits.ui.formatting import job_target, utc_short

settings = Settings.from_env()

st.markdown(
    theme.hero(
        "Exports",
        "Finished Shorts are separate MP4 files, saved per source video. "
        "Nothing is ever uploaded or published from here.",
    ),
    unsafe_allow_html=True,
)
st.code(f"{settings.exports_dir}/shorts/", language="text")

with closing(open_database(settings.db_path)) as conn:
    shorts = ShortRepository(conn)
    videos = {v.id: v for v in VideoRepository(conn).list_recent(limit=100_000)}
    projects = shorts.list_projects()
    renders = {p.id: shorts.list_renders(p.id, KIND_EXPORT) for p in projects}
    productions = [j for j in JobQueue(conn).list_recent(limit=200) if j.kind == PRODUCE_VIDEO]

tab_files, tab_runs = st.tabs(["Finished Shorts", "Production runs"])

with tab_files:
    by_video: dict[int, list[dict[str, object]]] = {}
    for project in projects:
        for render in renders[project.id]:
            by_video.setdefault(project.video_id, []).append(export_row(project.title, render))
    if not by_video:
        st.info(
            "No Shorts exported yet. Use **Import → Produce Shorts**, or open a clip in the "
            "Short editor and press Export."
        )
    for video_id, rows in by_video.items():
        video = videos.get(video_id)
        ready = sum(1 for r in rows if r["Status"] == "Ready")
        with st.expander(
            f"{video.original_filename if video else f'video #{video_id}'}  ·  "
            f"{ready} ready of {len(rows)}",
            expanded=len(by_video) == 1,
        ):
            st.dataframe(rows, hide_index=True, width="stretch")

with tab_runs:
    if not productions:
        st.caption("One-click productions will be summarised here.")
    for job in productions:
        rows = production_rows(job)
        ok = int((job.result or {}).get("succeeded", 0))
        head = f"Job {job.id} · {job_target(job)} · {job.status.value}"
        if rows:
            head += f" · {ok}/{len(rows)} Shorts"
        with st.expander(head):
            st.caption(f"{utc_short(job.created_at)} UTC")
            if job.status is JobStatus.FAILED and job.error:
                st.error(job.error)
            elif rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            elif job.result and job.result.get("note"):
                st.warning(str(job.result["note"]))
            else:
                st.caption(job.message or "No summary yet.")
