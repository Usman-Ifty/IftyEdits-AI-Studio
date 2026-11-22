"""Getting started: what the studio does and how to use it, as it works today."""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core.models import ALL_STAGES, JobStatus, StageStatus
from iftyedits.core.shorts import MAX_CLIP_S, MIN_CLIP_S
from iftyedits.core.sources import PERMISSION_NOTICE
from iftyedits.pipeline.jobs import PRODUCE_VIDEO, JobQueue
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import theme

settings = Settings.from_env()

st.markdown(
    theme.hero(
        "Getting started",
        "Turn a long video into several vertical Shorts. Everything runs on this computer.",
    ),
    unsafe_allow_html=True,
)

with closing(open_database(settings.db_path)) as conn:
    latest = VideoRepository(conn).list_recent(limit=1)
    if latest:
        vid = latest[0].id
        stages = ProcessingRepository(conn).stage_summary().get(vid, {})
        ready = all(
            stages.get(s) in (StageStatus.SUCCEEDED, StageStatus.SKIPPED) for s in ALL_STAGES
        )
        heard = vid in TranscriptRepository(conn).current_by_video()
        found = vid in ClipRepository(conn).current_by_video()
        exported = any(
            j.kind == PRODUCE_VIDEO
            and j.status is JobStatus.SUCCEEDED
            and int((j.result or {}).get("succeeded", 0)) > 0
            for j in JobQueue(conn).list_recent(limit=50)
        )
        progress = (True, ready, heard, found, exported)
        name = latest[0].original_filename
    else:
        progress, name = (False,) * 5, ""
st.markdown("**Your progress**" + (f" · latest video: {name}" if name else ""))
st.markdown(theme.workflow_tracker(progress), unsafe_allow_html=True)

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
            "Prefer to choose the moments yourself? Tick Review the picks before rendering; "
            "the studio lists what it would make and renders nothing until you run it again.",
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
        f"{MAX_CLIP_S:.0f} seconds. **Framing** has a crop slider and *Auto-frame*. "
        "Auto-frame looks at frames across the clip, picks the busiest part of the picture "
        "and, if it shifts, pans slowly with it (no jumps, a hard cut may reposition at "
        "once). It does not recognise faces or people, so check the crop guide. "
        "The mode shown above the slider tells you whether the crop is *Auto-follow* or "
        "*Manual*: moving the slider or the trim by hand switches to manual, and automatic "
        "production never overrides a crop you set. **Captions** has presets (Clean creator, "
        "Bold punchy, Cinematic, High contrast mobile and more), size, colours, position, "
        "optional key-word colouring and per-line text corrections. Make a quick preview "
        "before the full export."
    )
with st.expander("5. Export and find your files"):
    st.markdown(
        "Each Short is its own 1080x1920 MP4 with audio, checked after rendering. "
        "Existing files are never overwritten; a new version gets a new name."
    )
    st.code(f"{settings.exports_dir}/shorts/<video-name>-v<id>/", language="text")

with st.expander("What the scores mean"):
    st.markdown(
        "Each suggestion gets 0 to 100 from thirteen parts: hook, completeness, context, "
        "payoff, pacing, speech clarity, content density, emotion, energy and others. "
        "**75+ strong, 55+ promising, 35+ mixed, below weak.** The score says how well a "
        "moment stands alone; it cannot predict views, and it does not judge rights or "
        "platform rules. Open a suggestion on the Clips page to see every part, the "
        "strengths and the weaknesses."
    )
with st.expander("When something fails"):
    st.markdown(
        "- A failed Short does not stop the others; the Jobs page shows which ones failed "
        "and why in plain words.\n"
        "- Press **Retry** on a failed or cancelled job. Finished stages are reused, so only "
        "the missing work runs.\n"
        "- If the app or the worker was closed mid-job, start it again: interrupted jobs are "
        "recovered and no original is ever changed.\n"
        "- Exports are never overwritten; a repeat export gets a new file name."
    )

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
