"""Getting started: what the studio does and how to use it, as it works today."""

from __future__ import annotations

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.shorts import MAX_CLIP_S, MIN_CLIP_S
from iftyedits.core.sources import PERMISSION_NOTICE
from iftyedits.ui import theme

settings = Settings.from_env()

st.markdown(
    theme.hero(
        "Getting started",
        "Turn a long video into several vertical Shorts. Everything runs on this computer.",
    ),
    unsafe_allow_html=True,
)

st.subheader("The fast way")
st.markdown(
    theme.steps(
        [
            "Open Import. Paste a YouTube link, or choose a video file on this Mac.",
            "Press Produce Shorts. One job downloads or copies the video, prepares the "
            "audio, transcribes the speech, finds the best moments and renders each one.",
            "Watch the stages on Jobs. You can cancel at any time; finished Shorts are kept.",
            "Open Exports to see every finished MP4 and where it is saved.",
            "Want to change one? Open it in the Short editor to trim, re-frame, "
            "change the captions and export again.",
        ]
    ),
    unsafe_allow_html=True,
)

st.subheader("Step by step")
with st.expander("1. Get a video in"):
    st.markdown(
        "- **YouTube link:** paste a normal watch, `youtu.be` or Shorts link. Playlists, "
        "channels and live streams are not accepted.\n"
        "- **Local file:** type a path on this Mac. The file is copied into a protected, "
        "read-only vault; your original is never changed.\n"
        "- The same video is only stored once, even if you add it again."
    )
    st.info(PERMISSION_NOTICE)
with st.expander("2. Prepare and transcribe"):
    st.markdown(
        "Preparation extracts 16 kHz audio, a 720p proxy and a thumbnail. Transcription "
        "uses a speech model that runs locally. **The first run downloads the model** "
        "(Transcripts page shows sizes). Videos without speech produce no suggestions."
    )
with st.expander("3. Review clip suggestions"):
    st.markdown(
        "The Clips page ranks moments by score and shows **why** each scored as it did: "
        "hook, context, payoff, pacing and more. A score describes how well a moment "
        "stands on its own; it is **not** a prediction of views. Favourite or reject "
        "suggestions; rejected ones are skipped by automatic production."
    )
with st.expander("4. Edit a Short"):
    st.markdown(
        f"Open a suggestion in the Short editor. Length must be {MIN_CLIP_S:.0f} to "
        f"{MAX_CLIP_S:.0f} seconds. **Framing** has a crop slider and *Auto-frame*, "
        "which moves the crop over the busiest part of the picture (it does not "
        "recognise faces). **Captions** has presets, size, colours, position and "
        "per-line text corrections. Make a quick preview before the full export."
    )
with st.expander("5. Export and find your files"):
    st.markdown(
        "Each Short is its own 1080x1920 MP4 with audio, checked after rendering. "
        "Existing files are never overwritten; a new version gets a new name."
    )
    st.code(f"{settings.exports_dir}/shorts/<video-name>-v<id>/", language="text")

st.subheader("Good to know")
st.markdown(
    "- **Formats:** " + ", ".join(settings.video_extensions) + "\n"
    "- **Needs:** FFmpeg and FFprobe installed (`uv run iftyedits check` verifies them).\n"
    "- **Your data:** videos, database and exports stay under "
    f"`{settings.data_dir}`. Nothing is uploaded anywhere by this app.\n"
    "- **Rights:** you are responsible for having permission to reuse a video; the "
    "studio cannot judge copyright, fair use or platform rules.\n"
    "- **Common problems:** *no worker running* means start `uv run iftyedits worker` "
    "(the `ui` command starts one for you); *private or age-restricted video* cannot "
    "be fetched; *no suggestions* usually means little speech or a very low minimum "
    "score setting."
)
