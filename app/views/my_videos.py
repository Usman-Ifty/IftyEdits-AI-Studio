"""My Videos: imported source videos (library) and their transcripts, in one section."""

from __future__ import annotations

import streamlit as st

from iftyedits.ui import chrome, theme

st.markdown(
    theme.hero(
        "My Videos",
        "Your imported source videos. Originals are stored read-only and never modified.",
    ),
    unsafe_allow_html=True,
)
tab_library, tab_transcripts = st.tabs(["Videos", "Transcripts"])
with tab_library:
    chrome.run_view("library")
with tab_transcripts:
    chrome.run_view("transcripts")
