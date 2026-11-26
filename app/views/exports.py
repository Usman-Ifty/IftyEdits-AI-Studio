"""Exports: every finished Short, grouped by source video, plus production summaries."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import JobStatus
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.services.publishing import build_publishing_service
from iftyedits.storage.db import open_database
from iftyedits.storage.originals import is_within
from iftyedits.storage.shorts import KIND_EXPORT, ShortRender, ShortRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome, theme
from iftyedits.ui.exports import export_row, production_rows
from iftyedits.ui.formatting import human_size, job_target, utc_short

settings = Settings.from_env()

st.markdown(
    theme.hero(
        "Finished Shorts",
        "Finished Shorts are separate MP4 files, saved per source video. "
        "Nothing is ever uploaded or published from here.",
    ),
    unsafe_allow_html=True,
)
with st.expander("Where are the files?"):
    st.code(f"{settings.exports_dir}/shorts/", language="text", wrap_lines=True)

with closing(open_database(settings.db_path)) as conn:
    shorts = ShortRepository(conn)
    videos = {v.id: v for v in VideoRepository(conn).list_recent(limit=100_000)}
    projects = shorts.list_projects()
    renders = {p.id: shorts.list_renders(p.id, KIND_EXPORT) for p in projects}
    publisher = build_publishing_service(settings, conn)
    readiness_by_project = {p.id: publisher.readiness(p.id) for p in projects}
    productions = [j for j in JobQueue(conn).list_recent(limit=200) if j.kind == PRODUCE_VIDEO]

tab_files, tab_runs = st.tabs(["Gallery", "Production runs"])

PREVIEW_KEY = "fs-preview"


def _preview(path: str) -> None:
    st.session_state[PREVIEW_KEY] = path


def _open_package(project_id: int) -> None:
    st.session_state[chrome.PUBLISH_PROJECT_KEY] = project_id
    st.session_state[chrome.SECTION_KEY] = chrome.UPLOAD_SECTION
    st.session_state["fs-goto-editor"] = True


if st.session_state.pop("fs-goto-editor", False):
    st.switch_page("views/clip_editor.py")

with tab_files:
    ready_items: list[tuple[int, str, ShortRender, int]] = []
    problems: list[dict[str, object]] = []
    for project in projects:
        for render in renders[project.id]:
            if render.status == "succeeded" and render.output_path:
                ready_items.append((project.video_id, project.title, render, project.id))
            else:
                problems.append(export_row(project.title, render))
    st.markdown(
        theme.pill(f"{len(ready_items)} ready", "good" if ready_items else "muted")
        + (theme.pill(f"{len(problems)} failed or rendering", "warn") if problems else ""),
        unsafe_allow_html=True,
    )
    chosen = st.session_state.get(PREVIEW_KEY)
    if chosen:
        file = Path(chosen)
        _, middle, _ = st.columns([1, 1, 1])  # a 9:16 video at full width is too tall
        with middle, st.container(border=True):
            if file.is_file() and is_within(file, settings.exports_dir):
                st.video(str(file))
            else:
                st.warning("That file is no longer available in the exports folder.")
            st.button(
                "Close preview",
                key="fs-close",
                on_click=st.session_state.pop,
                args=(PREVIEW_KEY, None),
            )
    if not ready_items and not problems:
        st.markdown(
            theme.empty_state(
                "No Shorts yet",
                "Open Create Shorts to produce some, or finish one in the Clip Editor.",
            ),
            unsafe_allow_html=True,
        )
    ordered = sorted(ready_items, key=lambda item: item[2].finished_at or "", reverse=True)
    for row_start in range(0, len(ordered), 3):
        cols = st.columns(3)
        for col, (video_id, title, render, project_id) in zip(
            cols, ordered[row_start : row_start + 3], strict=False
        ):
            video = videos.get(video_id)
            meta = " · ".join(
                part
                for part in (
                    f"{render.width}x{render.height}" if render.width and render.height else "",
                    f"{render.duration_s:.0f}s" if render.duration_s else "",
                    human_size(render.size_bytes) if render.size_bytes else "",
                    utc_short(render.finished_at),
                )
                if part
            )
            with col:
                st.markdown(
                    theme.gallery_card(
                        title,
                        f"{video.original_filename if video else f'video #{video_id}'} · {meta}",
                        theme.pill("Rendered", "good")
                        + " "
                        + theme.pill(
                            readiness_by_project[project_id].label,
                            readiness_by_project[project_id].tone,
                        ),
                    ),
                    unsafe_allow_html=True,
                )
                path = str(render.output_path)
                st.button(
                    "Preview",
                    key=f"fs-prev-{render.id}",
                    on_click=_preview,
                    args=(path,),
                    icon=":material/play_arrow:",
                )
                st.button(
                    "Review upload package",
                    key=f"fs-pkg-{render.id}",
                    on_click=_open_package,
                    args=(project_id,),
                    icon=":material/inventory_2:",
                )
                with st.expander("File location"):
                    st.code(path, language="text", wrap_lines=True)
    if problems:
        with st.expander(f"Failed or still rendering ({len(problems)})"):
            st.dataframe(problems, hide_index=True, width="stretch")

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
