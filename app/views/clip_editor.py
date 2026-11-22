"""Clip Editor: review suggested candidates, then edit one as a Short (one section)."""

from __future__ import annotations

import streamlit as st

from iftyedits.ui import chrome, theme

st.markdown(
    theme.hero(
        "Clip Editor",
        "Review the moments the studio found, trim, frame, caption and export one as a Short, "
        "then prepare its upload package.",
    ),
    unsafe_allow_html=True,
)
SECTIONS = [chrome.REVIEW_SECTION, chrome.EDITOR_SECTION, chrome.UPLOAD_SECTION]
if st.session_state.get(chrome.SECTION_KEY) not in SECTIONS:
    st.session_state[chrome.SECTION_KEY] = chrome.REVIEW_SECTION
section = st.segmented_control(
    "Step", SECTIONS, key=chrome.SECTION_KEY, label_visibility="collapsed"
)
if section == chrome.EDITOR_SECTION:
    chrome.run_view("editor")
elif section == chrome.UPLOAD_SECTION:
    chrome.run_view("publish")
else:
    chrome.run_view("clips")
