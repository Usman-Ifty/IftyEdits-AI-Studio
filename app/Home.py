"""Streamlit entry point. Run with ``uv run iftyedits ui``.

Five everyday sections in workflow order, then three under "More". Several sections group
more than one view script (My Videos = Library + Transcripts, Clip Editor = Clips + Short
editor), so nothing is lost.
"""

from __future__ import annotations

import streamlit as st

from iftyedits.ui import chrome
from iftyedits.ui.theme import CSS, brand

st.set_page_config(page_title="IftyEdits AI Studio", page_icon="🎬", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.session_state[chrome.EMBED_KEY] = False  # every rerun starts on a stand-alone page

with st.sidebar:
    st.markdown(brand(), unsafe_allow_html=True)

# Everyday pages first; housekeeping sits under "More".
navigation = st.navigation(
    {
        "": [
            st.Page(
                "views/overview.py",
                title="Dashboard",
                icon=":material/space_dashboard:",
                default=True,
            ),
            st.Page("views/import_videos.py", title="Create Shorts", icon=":material/add_circle:"),
            st.Page("views/my_videos.py", title="My Videos", icon=":material/video_library:"),
            st.Page("views/clip_editor.py", title="Clip Editor", icon=":material/content_cut:"),
            st.Page("views/exports.py", title="Finished Shorts", icon=":material/movie:"),
        ],
        "More": [
            st.Page("views/jobs.py", title="Processing Queue", icon=":material/pending_actions:"),
            st.Page("views/settings.py", title="Settings", icon=":material/settings:"),
            st.Page("views/getting_started.py", title="Guides & Help", icon=":material/help:"),
        ],
    }
)
navigation.run()
