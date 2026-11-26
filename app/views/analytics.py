"""Analytics: how uploaded Shorts perform. Read only; numbers are fetched when you ask."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.errors import IftyEditsError
from iftyedits.services.analytics import AnalyticsService, build_analytics_service
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import ShortRepository
from iftyedits.ui import theme
from iftyedits.ui.analytics import comparison_rows, video_rows

settings = Settings.from_env()
FLASH = "analytics-flash"
NOT_LINKED = "Not linked to a Short"

st.markdown(
    theme.hero("Analytics", "See how your uploaded Shorts perform. Nothing on YouTube changes."),
    unsafe_allow_html=True,
)

for kind, text in st.session_state.pop(FLASH, []):
    {"success": st.success, "error": st.error, "warning": st.warning}.get(kind, st.info)(text)


def _flash(kind: str, text: str) -> None:
    st.session_state.setdefault(FLASH, []).append((kind, text))


def _service(conn: sqlite3.Connection) -> AnalyticsService:
    return build_analytics_service(settings, conn)


with closing(open_database(settings.db_path)) as conn:
    service = _service(conn)
    service.sync_uploads()
    status = service.status()
    videos = service.videos()
    comparisons = service.comparisons()
    projects = ShortRepository(conn).list_projects()

if not status.connected:
    st.info(f"{status.problem} Connect your YouTube account in Settings to fetch numbers.")

days = st.selectbox(
    "Time window for watch time and retention",
    [7, 28, 90, 365],
    index=1,
    format_func=lambda d: f"Last {d} days",
)
if st.button(
    "Refresh numbers",
    type="primary",
    disabled=not status.connected or not videos,
    help="Reads your video statistics from YouTube. It uses a little API quota.",
):
    try:
        with st.spinner("Asking YouTube..."), closing(open_database(settings.db_path)) as conn:
            report = _service(conn).refresh(int(days))
        _flash("success", f"Updated {report.updated} video(s).")
        for video_id, reason in report.skipped:
            _flash("warning", f"{video_id}: {reason}")
        if report.note:
            _flash("info", report.note)
    except IftyEditsError as exc:
        _flash("error", str(exc))
    st.rerun()

if not videos:
    st.markdown(
        theme.empty_state(
            "No videos to follow yet",
            "Uploads made by the studio appear here. For a Short you uploaded yourself, "
            "add its link below.",
        ),
        unsafe_allow_html=True,
    )
else:
    st.subheader("Your videos")
    st.dataframe(video_rows(videos), hide_index=True, width="stretch")
    st.caption(
        "Views, likes and comments are lifetime totals at the time of the last refresh. Watch "
        "time and retention cover the window shown and arrive a few days late."
    )
    st.subheader("What seems to work")
    st.caption(
        "Medians per editing choice, with the number of videos behind each. Small samples "
        "mislead; a pattern is not proof of cause."
    )
    for label, comparison in comparisons.items():
        with st.expander(label, expanded=comparison.comparable):
            st.write(comparison.note)
            rows = comparison_rows(comparison)
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")

with (
    st.expander("Add a video you uploaded yourself", expanded=not videos),
    st.form("analytics-add", clear_on_submit=True),
):
    reference = st.text_input(
        "YouTube link or video id", placeholder="https://youtube.com/shorts/..."
    )
    options = {NOT_LINKED: None} | {f"#{p.id}  {p.title}": p.id for p in projects}
    linked = st.selectbox(
        "Which Short is it?",
        list(options),
        help="Linking lets the comparisons use its caption style, length and framing.",
    )
    if st.form_submit_button("Follow this video"):
        try:
            with closing(open_database(settings.db_path)) as conn:
                added = _service(conn).add_video(reference, options[linked])
            _flash("success", f"Following {added}. Press Refresh numbers.")
        except IftyEditsError as exc:
            _flash("error", str(exc))
        st.rerun()

if videos:
    with st.expander("Stop following a video"):
        gone = st.selectbox("Video", [v.video_id for v in videos], key="analytics-remove")
        if st.button("Stop following", help="Deletes the stored numbers on this computer only."):
            with closing(open_database(settings.db_path)) as conn:
                _service(conn).remove_video(gone)
            _flash("success", f"Stopped following {gone}.")
            st.rerun()

if status.connected and not status.detailed:
    with st.expander("Allow detailed analytics (watch time and retention)"):
        st.write(
            "This opens Google's sign-in again and asks for read-only access to your YouTube "
            "Analytics. The **YouTube Analytics API** must also be enabled in your Google Cloud "
            "project."
        )
        if st.button("Open Google sign-in"):
            try:
                with (
                    st.spinner("Waiting for you to finish signing in..."),
                    closing(open_database(settings.db_path)) as conn,
                ):
                    _service(conn).connect_detailed()
                _flash("success", "Detailed analytics allowed.")
            except IftyEditsError as exc:
                _flash("error", str(exc))
            st.rerun()
