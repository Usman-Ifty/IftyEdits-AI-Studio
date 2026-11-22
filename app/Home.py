"""Streamlit entry point. Run with ``uv run iftyedits ui``."""

from __future__ import annotations

import streamlit as st

from iftyedits.ui.theme import CSS

st.set_page_config(page_title="IftyEdits AI Studio", page_icon="🎬", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)

navigation = st.navigation(
    {
        "Studio": [
            st.Page("views/overview.py", title="Overview", icon="🏠", default=True),
            st.Page("views/getting_started.py", title="Getting started", icon="🧭"),
        ],
        "Create": [
            st.Page("views/import_videos.py", title="Import", icon="⬇️"),
            st.Page("views/library.py", title="Library", icon="🎞️"),
            st.Page("views/transcripts.py", title="Transcripts", icon="📝"),
            st.Page("views/clips.py", title="Clips", icon="✨"),
            st.Page("views/editor.py", title="Short editor", icon="✂️"),
        ],
        "Output": [
            st.Page("views/exports.py", title="Exports", icon="📦"),
            st.Page("views/jobs.py", title="Jobs", icon="⚙️"),
        ],
    }
)
navigation.run()
