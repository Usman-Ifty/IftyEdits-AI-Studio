"""Jobs page: live progress of background work, with cancellation."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import CancelOutcome, JobStatus
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.storage.db import open_database
from iftyedits.ui.exports import production_rows
from iftyedits.ui.formatting import JOB_KIND_LABELS, job_row, job_status_label, job_target

settings = Settings.from_env()

st.title("Jobs")
st.caption(
    "Downloads, media preparation, transcription, analysis and Shorts production run in "
    "the background worker. This page refreshes every two seconds."
)

CANCEL_TEXT = {
    CancelOutcome.CANCELLED: "Job {id} cancelled before it started.",
    CancelOutcome.REQUESTED: "Cancellation of job {id} requested; the worker is stopping it.",
    CancelOutcome.NOT_ACTIVE: "Job {id} had already finished.",
    CancelOutcome.NOT_FOUND: "Job {id} no longer exists.",
}


@st.fragment(run_every=2)
def job_board() -> None:
    """Active jobs with cancel buttons, then recent history."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        active = queue.list_active()
        recent = queue.list_recent(limit=100)
        workers = queue.live_workers()

    if workers:
        st.success(f"Worker running: {', '.join(workers)}")
    elif active:
        st.warning("Jobs are waiting but no worker is running. Start `uv run iftyedits worker`.")

    st.subheader("Active")
    if not active:
        st.caption("Nothing is running or queued.")
    for job in active:
        with st.container(border=True):
            kind = JOB_KIND_LABELS.get(job.kind, job.kind)
            st.markdown(f"**Job {job.id}** · {kind} · {job_target(job)} · {job_status_label(job)}")
            st.progress(min(1.0, max(0.0, job.progress)), text=job.message or "Waiting")
            if job.cancel_requested_at:
                st.caption("Cancellation requested; waiting for the worker to stop.")
            elif st.button("Cancel", key=f"cancel-job-{job.id}"):
                with closing(open_database(settings.db_path)) as conn:
                    outcome = JobQueue(conn).request_cancel(job.id)
                st.info(CANCEL_TEXT[outcome].format(id=job.id))

    st.subheader("History")
    finished = [j for j in recent if j.status not in (JobStatus.QUEUED, JobStatus.RUNNING)]
    if not finished:
        st.caption("No finished jobs yet.")
        return
    st.dataframe(
        [job_row(j) for j in finished],
        hide_index=True,
        width="stretch",
        column_config={
            "Progress": st.column_config.ProgressColumn(
                min_value=0.0, max_value=1.0, format="percent"
            )
        },
    )
    productions = [j for j in finished if j.kind == PRODUCE_VIDEO and production_rows(j)]
    for job in productions[:5]:
        with st.expander(f"Production summary: job {job.id} ({job_target(job)})"):
            st.dataframe(production_rows(job), hide_index=True, width="stretch")
    failed = [j for j in finished if j.status is JobStatus.FAILED and j.error]
    if failed:
        with st.expander(f"Errors ({len(failed)})"):
            for job in failed:
                st.error(f"Job {job.id} ({job_target(job)}): {job.error}")


job_board()
