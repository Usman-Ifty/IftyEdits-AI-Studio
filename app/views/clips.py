"""Clips page: find, rank, inspect and review suggested Shorts for a transcribed video."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.clip_models import ClipCandidate, ClipDecision
from iftyedits.core.models import CancelOutcome, Job, JobStatus, Video
from iftyedits.pipeline.jobs import ANALYZE_CLIPS, TRANSCRIBE_VIDEO, JobQueue
from iftyedits.services.shorts import build_short_service
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome
from iftyedits.ui.clips import (
    DECISION_FILTERS,
    SORT_OPTIONS,
    ClipFilter,
    analysis_facts,
    card_reason,
    category_label,
    clip_row,
    component_rows,
    confidence_band,
    decision_label,
    filter_candidates,
    score_band,
    signal_row,
    signals_for,
    sort_candidates,
    span_label,
)
from iftyedits.ui.formatting import job_status_label, utc_short

settings = Settings.from_env()

chrome.heading("Clips")
st.caption(
    "Suggested moments from the speech, sound and scene changes. A score is not a "
    "prediction of views or a rights check. Nothing is uploaded here."
)


def _flash(kind: str, text: str) -> None:
    st.session_state.setdefault("flash-clips", []).append((kind, text))


def _show_flash() -> None:
    for kind, text in st.session_state.pop("flash-clips", []):
        (st.success if kind == "success" else st.info)(text)


def _cancel_button(job: Job) -> None:
    if job.cancel_requested_at:
        st.caption("Cancellation requested; waiting for the worker to stop.")
        return
    if st.button("Cancel analysis", key=f"cancel-{job.id}"):
        with closing(open_database(settings.db_path)) as conn:
            outcome = JobQueue(conn).request_cancel(job.id)
        done = outcome in (CancelOutcome.CANCELLED, CancelOutcome.REQUESTED)
        st.success("Cancellation requested." if done else "The job had already finished.")


@st.fragment(run_every=2)
def analysis_status(video: Video) -> None:
    """Progress, cancellation and last failure; refreshed every two seconds."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        active = queue.active_clip_analysis(video.id)
        latest = queue.latest(ANALYZE_CLIPS, video.id)
        workers = queue.live_workers()
    key = f"seen-clips-{video.id}"
    seen = st.session_state.get(key)
    st.session_state[key] = active.id if active else None
    if seen is not None and active is None:
        st.rerun()  # finished: redraw the whole page with the new results

    if active is not None:
        st.info(
            f"Job {active.id} is {job_status_label(active)}: "
            f"{active.message or 'waiting for a worker'} ({active.progress:.0%})"
        )
        st.progress(min(1.0, max(0.0, active.progress)))
        if not workers and active.status is JobStatus.QUEUED:
            st.warning("No worker is running. Start `uv run iftyedits worker`.")
        _cancel_button(active)
    elif latest is not None and latest.status is JobStatus.FAILED:
        st.error(
            f"Analysis job {latest.id} failed ({utc_short(latest.finished_at)} UTC): {latest.error}"
        )
    elif latest is not None and latest.status is JobStatus.CANCELLED:
        st.caption(f"Analysis job {latest.id} was cancelled. Earlier results are unchanged.")


@st.fragment(run_every=2)
def transcription_status(video: Video) -> None:
    """Start or follow this video's transcription without leaving the page."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        busy = [j for j in queue.list_active() if j.video_id == video.id]
        latest = queue.latest(TRANSCRIBE_VIDEO, video.id)
        workers = queue.live_workers()
        done = video.id in TranscriptRepository(conn).current_by_video()
    if done:
        st.rerun()
    if busy:
        job = busy[0]
        st.progress(
            min(1.0, max(0.0, job.progress)),
            text=f"Job {job.id} is {job_status_label(job)}: "
            f"{job.message or 'waiting for a worker'}",
        )
        if not workers:
            st.warning("No worker is running. Start `uv run iftyedits worker`.")
        return
    if latest is not None and latest.status is JobStatus.FAILED:
        st.error(f"Transcription job {latest.id} failed: {latest.error}")
    if st.button("Transcribe this video", key=f"clips-transcribe-{video.id}", type="primary"):
        language = settings.transcription_language
        with closing(open_database(settings.db_path)) as conn:
            JobQueue(conn).enqueue_transcription(
                video.id,
                model=settings.transcription_model,
                language=None if language == "auto" else language,
            )
        st.rerun()


with closing(open_database(settings.db_path)) as conn:
    videos = VideoRepository(conn).list_recent()
    transcripts = TranscriptRepository(conn).current_by_video()
    analyses = ClipRepository(conn).current_by_video()

if not videos:
    st.info("No videos imported yet. Open Create Shorts to add some.")
    chrome.halt()

labels = {f"#{v.id}  {v.original_filename}": v for v in videos}
# Open on the newest video that can show suggestions, not on one still waiting for a transcript.
ready = next((i for i, v in enumerate(videos) if v.id in transcripts), 0)
video = labels[st.selectbox("Video", list(labels), index=ready, key="clips-video")]
transcript = transcripts.get(video.id)

_show_flash()
if transcript is None:
    st.warning(
        "This video has no transcript yet. Transcribe it here or in My Videos > Transcripts."
    )
    transcription_status(video)
    chrome.halt()
if transcript.is_empty:
    st.warning("The transcript has no speech, so there is nothing to analyse.")
    chrome.halt()

analysis_status(video)
with closing(open_database(settings.db_path)) as conn:
    running = JobQueue(conn).active_clip_analysis(video.id)
existing = analyses.get(video.id)
left, right = st.columns([1, 3])
force = right.checkbox(
    "Analyse again",
    key=f"clips-force-{video.id}",
    help="Recompute even if a result for the same settings and transcript exists.",
)
if left.button(
    "Find clips" if existing is None else "Update suggestions",
    key=f"clips-run-{video.id}",
    type="primary",
    disabled=running is not None,
):
    with closing(open_database(settings.db_path)) as conn:
        job_id, created = JobQueue(conn).enqueue_clip_analysis(video.id, force=force)
    _flash(
        "success" if created else "info",
        f"Queued analysis job {job_id}." if created else f"Job {job_id} is already running.",
    )
    st.rerun()

if existing is None:
    st.info("No suggestions yet. Press Find clips; a background worker must be running.")
    chrome.halt()
analysis = existing
with closing(open_database(settings.db_path)) as conn:
    repo = ClipRepository(conn)
    candidates = repo.candidates(analysis.id)
    signals = repo.signals(analysis.id)

with st.expander("About this analysis"):
    for name, value in analysis_facts(analysis).items():
        st.markdown(f"**{name}:** {value}")
    if analysis.transcript_id != transcript.id:
        st.warning("The current transcript changed since this analysis. Press Update suggestions.")

st.subheader(f"Suggestions ({len(candidates)})")
if not candidates:
    st.info(
        "The analysis found no suitable moments. Very short or speech-free videos can do "
        "this. Try a video with more speech."
    )
    chrome.halt()

favorites = [c for c in candidates if c.decision is ClipDecision.FAVORITE]
if favorites and st.button(
    f"Export favourites ({len(favorites)})",
    key=f"clips-export-fav-{video.id}",
    icon=":material/movie:",
    help="Queues one MP4 per favourite with its saved settings. Open a clip with Edit first "
    "to change its framing or captions.",
):
    with closing(open_database(settings.db_path)) as conn:
        batch = build_short_service(settings, conn).export_candidates(
            video.id, [c.key for c in favorites]
        )
    summary = f"Queued {batch.queued} export(s)."
    if batch.reused:
        summary += f" {batch.reused} already finished or running."
    _flash("success" if not batch.failed else "info", summary)
    for _, reason in batch.failed:
        _flash("info", f"One clip was not queued: {reason}")
    st.rerun()

present = sorted({c.category for c in candidates})
filters = st.expander("Sort and filter")
f1, f2, f3, f4 = filters.columns(4)
sort = f1.selectbox(
    "Sort by", list(SORT_OPTIONS), format_func=SORT_OPTIONS.__getitem__, key="clips-sort"
)
kinds = f2.multiselect(
    "Moment type", present, format_func=category_label, key="clips-kinds",
    help="Empty shows every type.",
)  # fmt: skip
status = f3.selectbox("Status", list(DECISION_FILTERS), index=4, key="clips-status")
min_score = f4.slider("Minimum score", 0, 100, 0, key="clips-min-score")
d1, d2 = filters.columns(2)
min_len = d1.number_input("Shortest (s)", 0, 600, 0, key="clips-min-len")
max_len = d2.number_input("Longest (s), 0 = no limit", 0, 600, 0, key="clips-max-len")
shown = sort_candidates(
    filter_candidates(
        candidates,
        ClipFilter(
            categories=frozenset(kinds),
            min_score=float(min_score),
            min_duration_s=float(min_len),
            max_duration_s=float(max_len) if max_len else None,
            decision=status,
        ),
    ),
    sort,
)
st.caption(f"Showing {len(shown)} of {len(candidates)}.")
if not shown:
    st.info("No suggestion matches these filters.")
    chrome.halt()


def _label(c: ClipCandidate) -> str:
    return f"#{c.rank}  {c.title}  ({round(c.score)})"


def _inspect(c: ClipCandidate) -> None:
    st.session_state[f"clips-pick-{video.id}"] = _label(c)


def _open_editor(video_id: int, clip_key: str) -> None:
    st.session_state["editor-target"] = (video_id, clip_key)
    st.session_state[chrome.SECTION_KEY] = chrome.EDITOR_SECTION


def _thumb(c: ClipCandidate) -> object | None:
    """A still from the middle of the clip, or ``None`` when it cannot be read."""
    middle = (c.effective_start_s + c.effective_end_s) / 2
    try:
        with closing(open_database(settings.db_path)) as conn:
            return build_short_service(settings, conn).still(video.id, middle)
    except Exception:
        return None


st.markdown("**Best matches**")
top = shown[:6]
for row_start in range(0, len(top), 3):
    cols = st.columns(3)
    for col, cand in zip(cols, top[row_start : row_start + 3], strict=False):
        with col, st.container(border=True):
            thumb = _thumb(cand)
            if thumb is not None:
                st.image(str(thumb), width="stretch")
            st.markdown(
                f"**#{cand.rank} {cand.title}**  \n"
                f"Score **{round(cand.score)}** ({score_band(cand.score)}) · {span_label(cand)}"
            )
            st.caption(card_reason(cand, 90))
            edit_col, more_col = st.columns(2)
            if (
                edit_col.button(
                    "Edit",
                    key=f"cardedit-{cand.id}",
                    type="primary",
                    width="stretch",
                    on_click=_open_editor if chrome.embedded() else None,
                    args=(video.id, cand.key) if chrome.embedded() else None,
                )
                and not chrome.embedded()
            ):
                _open_editor(video.id, cand.key)
                st.switch_page("views/editor.py")
            more_col.button(
                "Details",
                key=f"card-{cand.id}",
                on_click=_inspect,
                args=(cand,),
                width="stretch",
                help="Shows the full reasons and the score breakdown below.",
            )
with st.expander(f"All {len(shown)} suggestions as a table"):
    st.dataframe([clip_row(c) for c in shown], hide_index=True, width="stretch")


def _decide(candidate: ClipCandidate, decision: ClipDecision | None, text: str) -> None:
    with closing(open_database(settings.db_path)) as conn:
        ClipRepository(conn).decide(candidate.id, decision)
    _flash("success", text)
    st.rerun()


options = {_label(c): c for c in shown}
if st.session_state.get(f"clips-pick-{video.id}") not in options:
    st.session_state.pop(f"clips-pick-{video.id}", None)  # a filter hid the earlier choice
picked = options[st.selectbox("Inspect", list(options), key=f"clips-pick-{video.id}")]
st.markdown(
    f"### {picked.title}\n"
    f"**{span_label(picked)}** · {category_label(picked.category)} · "
    f"score **{round(picked.score)}** ({score_band(picked.score)}) · "
    f"{confidence_band(picked.confidence)} · {decision_label(picked.decision) or 'undecided'}"
)
st.write(picked.explanation)
st.markdown(f"> {picked.excerpt}")
with st.expander("Why this score"):
    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**Strengths**")
        for line in picked.strengths or ("none noted",):
            st.markdown(f"- {line}")
    with s2:
        st.markdown("**Weaknesses**")
        for line in picked.weaknesses or ("none noted",):
            st.markdown(f"- {line}")
    st.markdown("**Score breakdown** (value 0-100; weight 0 means not assessed)")
    st.dataframe(component_rows(picked), hide_index=True, width="stretch")
    st.markdown("**Evidence signals**")
    related = signals_for(picked, signals)
    if related:
        st.dataframe([signal_row(s) for s in related], hide_index=True, width="stretch")
    else:
        st.caption("No signals fall inside this clip.")
    st.caption(f"Built by: {', '.join(picked.methods)}. Id {picked.key}.")


if st.button(
    "Edit as a Short",
    key=f"edit-{picked.id}",
    type="primary",
    on_click=_open_editor if chrome.embedded() else None,
    args=(video.id, picked.key) if chrome.embedded() else None,
):
    # standalone page (not inside the Clip Editor section): go to the editor page
    _open_editor(video.id, picked.key)
    st.switch_page("views/editor.py")

b1, b2, b3 = st.columns(3)
if b1.button(
    "★ Favorite", key=f"fav-{picked.id}", disabled=picked.decision is ClipDecision.FAVORITE
):
    _decide(
        picked,
        ClipDecision.FAVORITE,
        "Marked as favorite. Re-analysis will rank similar moments higher.",
    )
if b2.button("✖ Reject", key=f"rej-{picked.id}", disabled=picked.decision is ClipDecision.REJECTED):
    _decide(picked, ClipDecision.REJECTED, "Rejected. Re-analysis will rank similar moments lower.")
if b3.button("Clear decision", key=f"clr-{picked.id}", disabled=picked.decision is None):
    _decide(picked, None, "Decision cleared.")
