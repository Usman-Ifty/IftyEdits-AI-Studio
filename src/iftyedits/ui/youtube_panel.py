"""The YouTube tab of the Upload package page and the connection card in Settings.

Draws the exact details that will be uploaded and collects the owner's typed approval. All
decisions live in :mod:`iftyedits.services.youtube_upload`; this module only displays and calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from iftyedits.config import Settings
from iftyedits.core import youtube as yt
from iftyedits.core.errors import IftyEditsError
from iftyedits.services.youtube_upload import YouTubeUploadService, build_youtube_service
from iftyedits.storage.db import open_database
from iftyedits.storage.youtube import UploadRecord
from iftyedits.ui import theme
from iftyedits.ui.formatting import human_size, utc_short

_TONES = {
    yt.DRAFT: "warn",
    yt.APPROVED: "accent",
    yt.UPLOADING: "accent",
    yt.UPLOADED: "good",
    yt.FAILED: "bad",
    yt.CANCELLED: "muted",
}


def _service(settings: Settings) -> tuple[YouTubeUploadService, sqlite3.Connection]:
    conn = open_database(settings.db_path)
    return build_youtube_service(settings, conn), conn


def _flash(key: str) -> None:
    message = st.session_state.pop(f"{key}-ok", None)
    if message:
        st.success(message)
    error = st.session_state.pop(f"{key}-err", None)
    if error:
        st.error(error)


def _show_url(slot: DeltaGenerator, url: str) -> None:
    slot.markdown(f"If no tab opened, [open sign-in]({url})")


def connection_card(settings: Settings) -> None:
    """Connect / disconnect, used in Settings and in the YouTube tab."""
    service, conn = _service(settings)
    try:
        state = service.connection()
    finally:
        conn.close()
    if not state.configured:
        st.markdown(theme.pill("Not set up", "muted"), unsafe_allow_html=True)
        st.write(
            "YouTube upload is optional. You can always use **Upload package → Package** to "
            "copy everything into YouTube yourself."
        )
        st.caption(state.problem)
        st.caption("Setup steps: docs/YOUTUBE.md")
        return
    if not state.connected:
        st.markdown(theme.pill("Configured, not connected", "warn"), unsafe_allow_html=True)
        st.caption("Connecting opens Google's sign-in page in your browser.")
        slot = st.empty()
        if st.button("Connect YouTube account", key="yt-connect", icon=":material/link:"):
            try:
                with st.spinner("Waiting for you to finish signing in in your browser…"):
                    service2, conn2 = _service(settings)
                    try:
                        service2.connect(on_url=lambda u: _show_url(slot, u))
                    finally:
                        conn2.close()
            except IftyEditsError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        return
    st.markdown(theme.pill("Connected", "good"), unsafe_allow_html=True)
    st.caption(f"Token file: {state.token_location} (outside the project, owner-only access)")
    if st.button("Disconnect", key="yt-disconnect", icon=":material/link_off:"):
        service2, conn2 = _service(settings)
        try:
            service2.disconnect()
        finally:
            conn2.close()
        st.rerun()


def render(settings: Settings, project_id: int) -> None:
    """The whole YouTube tab for one Short."""
    st.markdown(
        theme.hero(
            "Upload to YouTube (optional)",
            "Review exactly what will be sent, then approve it. The video is uploaded as "
            "Private or Unlisted only. The studio never publishes or schedules.",
        ),
        unsafe_allow_html=True,
    )
    connection_card(settings)
    _flash("yt")

    service, conn = _service(settings)
    try:
        state = service.connection()
        if not (state.configured and state.connected):
            return
        _status_block(settings, project_id)
    finally:
        conn.close()


def _act(settings: Settings, action: Callable[[YouTubeUploadService], object], ok: str) -> None:
    service, conn = _service(settings)
    try:
        action(service)
    except IftyEditsError as exc:
        st.session_state["yt-err"] = str(exc)
    else:
        st.session_state["yt-ok"] = ok
    finally:
        conn.close()


def _status_block(settings: Settings, project_id: int) -> None:
    @st.fragment(run_every=3)
    def block() -> None:
        service, conn = _service(settings)
        try:
            records = service.records(project_id)
            latest = records[0] if records else None
            active = latest is not None and latest.status in (yt.APPROVED, yt.UPLOADING)
            report = service.publishing.readiness(project_id)
            ready = report.status == "ready"
            status = latest.status if latest else ""
            st.markdown(
                theme.upload_tracker(
                    (
                        ready or report.can_confirm,
                        ready,
                        status in (yt.DRAFT, yt.APPROVED, yt.UPLOADING, yt.UPLOADED),
                        status in (yt.APPROVED, yt.UPLOADING, yt.UPLOADED),
                        status == yt.UPLOADED,
                    )
                ),
                unsafe_allow_html=True,
            )
            st.caption(_next_hint(ready, report.can_confirm, report.label, status))
            if not active and latest is not None and latest.status == yt.DRAFT:
                _draft(settings, service, latest)
            elif latest is not None and active:
                _progress(settings, latest)
            else:
                if latest is not None:
                    _outcome(settings, latest)
                st.markdown("#### Prepare an upload")
                if report.label and report.status != "ready":
                    st.info(
                        f"Status: {report.label}. Finish and confirm the checklist in the "
                        "Checklist tab first; uploads are only possible once it is confirmed."
                    )
                if st.button(
                    "Review upload details",
                    key=f"yt-prepare-{project_id}",
                    disabled=report.status != "ready",
                    icon=":material/fact_check:",
                ):
                    _act(
                        settings,
                        lambda s: _prepare(s, project_id),
                        "Upload details prepared. Check them below.",
                    )
                    st.rerun()
            if len(records) > 1 or (records and not active):
                with st.expander("Upload history"):
                    st.dataframe(
                        [
                            {
                                "When (UTC)": utc_short(r.created_at),
                                "Status": r.label,
                                "Visibility": r.details.get("visibility", ""),
                                "Title": r.details.get("title", ""),
                                "YouTube video": r.youtube_video_id or "",
                            }
                            for r in records
                        ],
                        hide_index=True,
                        width="stretch",
                    )
        finally:
            conn.close()

    block()


def _next_hint(ready: bool, can_confirm: bool, label: str, status: str) -> str:
    """One plain sentence about what to do now."""
    if status == yt.DRAFT:
        return "Check every detail below. Nothing is sent until you type the approval sentence."
    if status == yt.APPROVED:
        return "Approved. The background worker will start the upload shortly."
    if status == yt.UPLOADING:
        return "Uploading now. You can cancel; nothing is published either way."
    if status == yt.UPLOADED:
        return "Done. Open it in YouTube Studio to check it and publish it yourself."
    if ready:
        return (
            "Your checklist is confirmed. Press “Review upload details” to see what would be sent."
        )
    if can_confirm:
        return "The checklist is complete. Press “Confirm” in the Checklist tab, then come back."
    return (
        f"Status: {label}. Finish the Metadata, Safety review, Rights and Checklist tabs "
        "first, then confirm in the Checklist tab."
    )


def _prepare(service: YouTubeUploadService, project_id: int) -> None:
    result = service.prepare(project_id)
    if result.problems:
        raise IftyEditsError("Cannot upload yet: " + " ".join(result.problems))


def _draft(settings: Settings, service: YouTubeUploadService, record: UploadRecord) -> None:
    d = record.details
    st.markdown("#### Check every detail, then approve")
    st.markdown(theme.pill(record.label, _TONES[record.status]), unsafe_allow_html=True)
    st.table(
        {
            "Destination channel": f"{d['channel_title']} ({d['channel_id']})",
            "Video file": f"{d['file_name']} · {human_size(int(d['file_size']))}",
            "File fingerprint (SHA-256)": str(d["file_sha256"])[:16] + "…",
            "Visibility": str(d["visibility"]).capitalize(),
            "Made for kids": "Yes" if d["made_for_kids"] else "No",
            "Title": str(d["title"]),
        }
    )
    st.text_area(
        "Description (exactly as sent)",
        str(d["description"]),
        disabled=True,
        height=160,
        key=f"yt-desc-{record.id}",
    )
    st.caption("Search tags: " + (", ".join(d["tags"]) or "none"))
    st.warning(
        "Nothing has been sent yet. After approval the video is uploaded as "
        f"{d['visibility']}; you publish it yourself in YouTube Studio. Uploading uses your "
        "YouTube API quota."
    )
    typed = st.text_input(
        f"Type “{yt.APPROVAL_TEXT}” to approve this exact upload",
        key=f"yt-typed-{record.id}",
    )
    again = st.checkbox(
        "Upload again even if this exact video was uploaded before", key=f"yt-dup-{record.id}"
    )
    c1, c2 = st.columns(2)
    if c1.button(
        "Approve and upload",
        type="primary",
        key=f"yt-approve-{record.id}",
        disabled=typed.strip() != yt.APPROVAL_TEXT,
        icon=":material/cloud_upload:",
    ):
        _act(
            settings,
            lambda s: s.approve(record.id, typed, allow_duplicate=again),
            "Approved. The upload is queued; keep the studio running.",
        )
        st.rerun()
    if c2.button("Discard this draft", key=f"yt-discard-{record.id}"):
        _act(settings, lambda s: s.cancel(record.id), "Draft discarded.")
        st.rerun()


def _progress(settings: Settings, record: UploadRecord) -> None:
    total = int(record.details.get("file_size", 0)) or 1
    st.markdown(theme.pill(record.label, _TONES[record.status]), unsafe_allow_html=True)
    st.progress(
        min(1.0, record.bytes_sent / total),
        text=f"{human_size(record.bytes_sent)} of {human_size(total)}",
    )
    st.caption("Needs the studio's background worker (`iftyedits ui` starts one).")
    label = "Cancel before it starts" if record.status == yt.APPROVED else "Cancel upload"
    if st.button(label, key=f"yt-cancel-{record.id}"):
        _act(settings, lambda s: s.cancel(record.id), "Cancel requested.")
        st.rerun()


def _outcome(settings: Settings, record: UploadRecord) -> None:
    cancelled = record.status == yt.FAILED and record.error_kind == yt.KIND_CANCELLED
    if cancelled:
        st.markdown(theme.pill("Cancelled by you", "muted"), unsafe_allow_html=True)
    else:
        st.markdown(theme.pill(record.label, _TONES[record.status]), unsafe_allow_html=True)
    if record.status == yt.UPLOADED and record.studio_url:
        st.success(
            f"Uploaded as {record.details.get('visibility')}. It is NOT public. "
            f"[Open in YouTube Studio]({record.studio_url}) to check and publish it."
        )
    if cancelled:
        st.info(record.error or "You cancelled this upload. Nothing was published.")
    elif record.status == yt.FAILED:
        st.error(record.error or "The upload failed.")
        if record.error_kind == yt.KIND_QUOTA:
            st.caption(
                "Quota errors clear on their own; try again later. Manual export still works."
            )
    if record.status == yt.FAILED and record.approved_at and not record.youtube_video_id:
        label = "Start this approved upload again" if cancelled else "Retry this approved upload"
        if st.button(label, key=f"yt-retry-{record.id}", icon=":material/refresh:"):
            _act(
                settings,
                lambda s: s.retry(record.id),
                "Queued again. Everything is re-checked before it uploads.",
            )
            st.rerun()
