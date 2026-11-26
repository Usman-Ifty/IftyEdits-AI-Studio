"""Import page: choose a local file or folder, queue it, watch progress.

Copying and probing run in the background worker, never in this script.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.errors import IftyEditsError
from iftyedits.core.sources import PERMISSION_NOTICE, parse_video_url
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import discover, validate_source
from iftyedits.services.production import ProductionOptions
from iftyedits.storage.db import open_database
from iftyedits.ui import theme
from iftyedits.ui.formatting import human_size, job_row
from iftyedits.ui.shorts import CAPTION_PRESET_LABELS

settings = Settings.from_env()
SCAN_KEY = "import_scan"

st.markdown(
    theme.hero(
        "Create Shorts",
        "Paste a YouTube link to produce Shorts in one go, or add a video file from this Mac.",
    ),
    unsafe_allow_html=True,
)
tab_url, tab_file = st.tabs(["YouTube link", "Local file"])

with tab_url:
    st.caption(PERMISSION_NOTICE)
    with st.form("url"):
        raw_url = st.text_input(
            "YouTube video link",
            placeholder="https://www.youtube.com/watch?v=...",
            help="A normal watch, youtu.be or Shorts link. Playlists, channels and live "
            "streams are not accepted.",
        )
        with st.expander("Options (the defaults work well)"):
            o1, o2, o3 = st.columns(3)
            clips = o1.number_input(
                "Most Shorts to make", 1, 20, 5, help="Only the best-scoring suggestions are used."
            )
            min_score = o2.slider(
                "Minimum score", 0, 100, 40, help="Suggestions scoring below this are skipped."
            )
            preset = o3.selectbox(
                "Caption style",
                list(CAPTION_PRESET_LABELS),
                index=list(CAPTION_PRESET_LABELS).index("highlight"),
                format_func=lambda key: CAPTION_PRESET_LABELS[key],
            )
            review = st.checkbox(
                "Review the picks before rendering",
                help="Selects the best clips and stops. Check them in the Clip Editor, then "
                "produce again without this box.",
            )
        allowed = st.checkbox("I have permission to use this video")
        b1, b2 = st.columns(2)
        produce = b1.form_submit_button("Produce Shorts", type="primary")
        download = b2.form_submit_button("Download only")
    if produce or download:
        try:
            if not allowed:
                raise IftyEditsError("Tick the permission box to continue.")
            video_url = parse_video_url(raw_url)
            with closing(open_database(settings.db_path)) as conn:
                queue = JobQueue(conn)
                if produce:
                    options = ProductionOptions(
                        max_clips=int(clips),
                        min_score=float(min_score),
                        caption_preset=preset,
                        review_only=review,
                    ).validate()
                    job_id, created = queue.enqueue_production(
                        options.as_payload(), url=video_url.canonical
                    )
                else:
                    job_id, created = queue.enqueue_url_fetch(video_url.canonical, process=True)
            st.success(
                f"Job {job_id} {'queued' if created else 'is already queued for this link'}. "
                "Follow it on the Processing Queue page."
            )
        except (IftyEditsError, ValueError) as exc:
            st.error(str(exc))

with tab_file:
    st.write(
        "Enter a path on this Mac. Files are copied into the protected vault; "
        "your originals are only read, never changed, moved or deleted."
    )

    with st.form("scan"):
        raw_path = st.text_input(
            "File or folder path", placeholder="/Users/you/Movies/Episode 12.mp4"
        )
        recursive = st.checkbox("Include subfolders", value=False)
        scanned = st.form_submit_button("Scan")

    if scanned:
        st.session_state.pop(SCAN_KEY, None)
        path = Path(raw_path.strip()).expanduser()
        try:
            if not raw_path.strip():
                raise IftyEditsError("Enter a path first.")
            if path.is_dir():
                found = discover(settings, path, recursive=recursive)
                files = list(found.files)
                skipped = [(str(p), reason) for p, reason in found.skipped]
            else:
                files = [validate_source(settings, path)]
                skipped = []
            st.session_state[SCAN_KEY] = {"files": [str(f) for f in files], "skipped": skipped}
        except IftyEditsError as exc:
            st.error(str(exc))

    scan = st.session_state.get(SCAN_KEY)
    if scan:
        files = [Path(f) for f in scan["files"]]
        if scan["skipped"]:
            st.warning(f"{len(scan['skipped'])} item(s) in this folder will not be imported.")
            with st.expander("Skipped items and reasons"):
                st.dataframe(
                    [{"Path": p, "Reason": reason} for p, reason in scan["skipped"]],
                    hide_index=True,
                    width="stretch",
                )
        if not files:
            st.info("No video files found.")
        else:
            st.dataframe(
                [
                    {"File": f.name, "Folder": str(f.parent), "Size": human_size(f.stat().st_size)}
                    for f in files
                ],
                hide_index=True,
                width="stretch",
            )
            prepare = st.checkbox(
                "Prepare media after import (16 kHz audio, 720p proxy, thumbnail)", value=True
            )
            if st.button(f"Queue {len(files)} file(s) for import", type="primary"):
                with closing(open_database(settings.db_path)) as conn:
                    ids = JobQueue(conn).enqueue_imports(files, process_after=prepare)
                st.session_state.pop(SCAN_KEY, None)
                already = len(files) - len(ids)
                st.success(
                    f"Queued {len(ids)} import(s)."
                    + (f" {already} were already queued." if already else "")
                )


@st.fragment(run_every=2)
def job_status() -> None:
    """Live job table, refreshed every two seconds without rerunning the page."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        jobs = queue.list_recent(limit=5)
        workers = queue.live_workers()
    st.subheader("Recent jobs")
    if not workers and any(j.status.value == "queued" for j in jobs):
        st.warning("Jobs are waiting but no worker is running. Start `uv run iftyedits worker`.")
    if not jobs:
        st.caption("No jobs yet.")
        return
    st.caption("Cancel jobs and see processing errors on the Processing Queue page.")
    st.dataframe(
        [job_row(j) for j in jobs],
        hide_index=True,
        width="stretch",
        column_order=("ID", "Kind", "File", "Status", "Progress", "Detail"),
        column_config={
            "Progress": st.column_config.ProgressColumn(
                min_value=0.0, max_value=1.0, format="percent"
            )
        },
    )


job_status()
