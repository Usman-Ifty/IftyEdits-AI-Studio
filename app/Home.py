"""Streamlit entry point. Run with ``uv run iftyedits ui``."""

from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="IftyEdits AI Studio", layout="wide")

navigation = st.navigation(
    [
        st.Page("views/overview.py", title="Overview", default=True),
        st.Page("views/import_videos.py", title="Import"),
        st.Page("views/library.py", title="Library"),
        st.Page("views/transcripts.py", title="Transcripts"),
        st.Page("views/clips.py", title="Clips"),
        st.Page("views/jobs.py", title="Jobs"),
    ]
)
navigation.run()
