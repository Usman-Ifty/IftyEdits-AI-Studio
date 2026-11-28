"""Dashboard: what is in the studio, what is running and what to do next."""

from __future__ import annotations

from contextlib import closing
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from iftyedits.config import Settings
from iftyedits.core.models import ALL_STAGES, JobStatus, StageStatus
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.services.journey import latest_progress
from iftyedits.services.preferences import Preferences
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import theme
from iftyedits.ui.formatting import (
    JOB_KIND_LABELS,
    human_duration,
    human_size,
    job_target,
    utc_short,
    video_title,
)

settings = Settings.from_env()
prefs = Preferences(settings.data_dir)


def link(page: str, label: str, icon: str, target: Any = st) -> None:
    """A page link; shows plain text if the page is not registered (e.g. in tests)."""
    try:
        target.page_link(page, label=label, icon=icon)
    except StreamlitPageNotFoundError:
        target.caption(f"{icon} {label}")


st.markdown(
    theme.hero("Dashboard", "Turn a long video into vertical Shorts with captions."),
    unsafe_allow_html=True,
)

with closing(open_database(settings.db_path)) as conn:
    videos = VideoRepository(conn).list_recent(limit=100_000)
    queue = JobQueue(conn)
    workers = queue.live_workers()
    active = queue.count_active()
    summary = ProcessingRepository(conn).stage_summary()
    transcribed_ids = set(TranscriptRepository(conn).current_by_video())
    analysed = len(ClipRepository(conn).current_by_video())
    recent_jobs = queue.list_recent(limit=50)

names = {v.id: v.original_filename for v in videos}
done = (StageStatus.SUCCEEDED, StageStatus.SKIPPED)
prepared = sum(1 for v in videos if all(summary.get(v.id, {}).get(s) in done for s in ALL_STAGES))
failed = sum(1 for v in videos if StageStatus.FAILED in summary.get(v.id, {}).values())
productions = [
    j for j in recent_jobs if j.kind == PRODUCE_VIDEO and j.status is JobStatus.SUCCEEDED
]
made = sum(int((j.result or {}).get("succeeded", 0)) for j in productions)

if not videos and not prefs.get("welcome_dismissed"):
    with st.container(border=True):
        st.markdown("#### New here?")
        st.write(
            "Open Create Shorts, press **Produce Shorts**, and the studio does the rest. "
            "Guides & Help explains each step."
        )
        link("views/getting_started.py", "Read the guide", ":material/help:")
        if st.button("Hide this welcome", help="You can always reopen Guides & Help."):
            prefs.set("welcome_dismissed", True)
            st.rerun()

go_create, go_watch = st.columns(2)
with go_create, st.container(border=True):
    st.markdown("#### 1. Make Shorts")
    st.caption("Paste a link or add a video file.")
    link("views/import_videos.py", "Create Shorts", ":material/add_circle:")
with go_watch, st.container(border=True):
    st.markdown("#### 2. Watch and upload")
    st.caption("Your finished Shorts, ready to check.")
    link("views/exports.py", "Finished Shorts", ":material/movie:")

if not workers:
    st.warning(
        "No background worker is running, so queued jobs will wait. Start one with "
        "`uv run iftyedits worker`, or `uv run iftyedits ui --start-worker` while this "
        "dashboard stays open."
    )
elif not active:
    st.markdown(theme.pill("Ready", "good"), unsafe_allow_html=True)


@st.fragment(run_every=2)
def live_activity(seen: int) -> None:
    """What the worker is doing now, refreshed every two seconds."""
    with closing(open_database(settings.db_path)) as live_conn:
        running = JobQueue(live_conn).list_active()
    if len(running) < seen:
        st.rerun()  # something finished: redraw the lists below
    st.subheader("Working now")
    for job in running[:3]:
        with st.container(border=True):
            kind = JOB_KIND_LABELS.get(job.kind, job.kind)
            kind = kind[:1].upper() + kind[1:]
            st.markdown(f"**{kind}** · {job_target(job, names)}")
            st.progress(min(1.0, max(0.0, job.progress)), text=job.message or "Waiting")
    st.caption(f"Working on {len(running)} job(s) now.")
    link("views/jobs.py", "See progress", ":material/pending_actions:")


if active:
    live_activity(active)
if failed:
    st.error(f"{failed} video(s) have a failed preparation stage. See My Videos.")
elif videos and prepared < len(videos):
    st.info(f"{len(videos) - prepared} video(s) still need media preparation (My Videos).")

left, right = st.columns([3, 2], gap="large")
with left:
    st.subheader("Recent videos")
    if not videos:
        st.caption("Nothing here yet. Import a video to get started.")
    for video in videos[:3]:
        badge = theme.pill("Transcribed", "good") if video.id in transcribed_ids else ""
        st.markdown(
            theme.card(
                video_title(video.original_filename),
                f"#{video.id} · {human_size(video.size_bytes)} · "
                f"{human_duration(video.duration_s)}",
                badge,
            ),
            unsafe_allow_html=True,
        )
with right:
    st.subheader("Recent exports")
    if not productions:
        st.caption("Finished Shorts appear here after a production.")
    for job in productions[:3]:
        result = job.result or {}
        ok, total = int(result.get("succeeded", 0)), len(result.get("clips", []))
        tone = "good" if total and ok == total else "warn"
        st.markdown(
            theme.card(
                job_target(job, names),
                utc_short(job.finished_at),
                theme.pill(f"{ok}/{total} Shorts", tone),
            ),
            unsafe_allow_html=True,
        )

with st.expander("Studio details"):
    with closing(open_database(settings.db_path)) as _conn:
        _flags, _name = latest_progress(_conn)
    st.markdown("**Workflow**" + (f" · latest video: {_name}" if _name else ""))
    st.markdown(theme.journey(_flags), unsafe_allow_html=True)
    row1 = st.columns(3)
    row1[0].metric("Videos", len(videos), help="Originals stored in the protected vault")
    row1[1].metric("Media prepared", f"{prepared} / {len(videos)}")
    row1[2].metric("Transcribed", f"{len(transcribed_ids)} / {len(videos)}")
    row2 = st.columns(3)
    row2[0].metric("Clips analysed", f"{analysed} / {len(videos)}")
    row2[1].metric("Shorts produced", made, help="From recent one-click productions")
    row2[2].metric("Active jobs", active)
    st.code(
        f"data      {settings.data_dir}\n"
        f"originals {settings.originals_dir}  (read-only vault)\n"
        f"derived   {settings.derived_dir}  (audio, proxies, thumbnails)\n"
        f"exports   {settings.exports_dir}  (finished Shorts, transcript exports)\n"
        f"models    {settings.models_dir}  (speech models, downloaded on request)\n"
        f"database  {settings.db_path}",
        language="text",
    )
