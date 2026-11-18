"""Import page: choose a local file or folder, queue it, watch progress.

Copying and probing run in the background worker, never in this script.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.errors import IftyEditsError
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.ingest import discover, validate_source
from iftyedits.storage.db import open_database
from iftyedits.ui.formatting import human_size, job_row

settings = Settings.from_env()
SCAN_KEY = "import_scan"

st.title("Import videos")
st.write(
    "Enter a path on this Mac. Files are copied into the protected vault; "
    "your originals are only read, never changed, moved or deleted."
)

with st.form("scan"):
    raw_path = st.text_input("File or folder path", placeholder="/Users/you/Movies/Episode 12.mp4")
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
        if st.button(f"Queue {len(files)} file(s) for import", type="primary"):
            with closing(open_database(settings.db_path)) as conn:
                ids = JobQueue(conn).enqueue_imports(files)
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
        jobs = queue.list_recent(limit=50)
        workers = queue.live_workers()
    st.subheader("Jobs")
    if not workers and any(j.status.value == "queued" for j in jobs):
        st.warning("Jobs are waiting but no worker is running. Start `uv run iftyedits worker`.")
    if not jobs:
        st.caption("No jobs yet.")
        return
    st.dataframe(
        [job_row(j) for j in jobs],
        hide_index=True,
        width="stretch",
        column_config={
            "Progress": st.column_config.ProgressColumn(
                min_value=0.0, max_value=1.0, format="percent"
            )
        },
    )


job_status()
