"""Short editor: trim a suggested clip, frame it 9:16, caption it, preview and export."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from typing import Any

import streamlit as st
from PIL import Image

from iftyedits.adapters.faces import load_face_detector
from iftyedits.adapters.short_render import crop_guide, framed_still
from iftyedits.config import Settings
from iftyedits.core.captions import CaptionCue, clean_text
from iftyedits.core.errors import IftyEditsError, ShortConfigError
from iftyedits.core.framing import position_at
from iftyedits.core.models import JobStatus
from iftyedits.core.shorts import (
    CAPTION_PRESETS,
    MAX_CLIP_S,
    MIN_CLIP_S,
    SAFE_BOTTOM,
    SAFE_TOP,
    AudioSettings,
    CaptionStyle,
    OutputSettings,
    PolishSettings,
    ShortConfig,
    compute_crop,
)
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.shorts import ShortService, SourceInfo, build_short_service
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import KIND_EXPORT, KIND_PREVIEW, ShortProject, ShortRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome
from iftyedits.ui.clips import category_label, span_label
from iftyedits.ui.formatting import human_size
from iftyedits.ui.shorts import (
    CAPTION_PRESET_LABELS,
    QUALITY_LABELS,
    SIZE_LABELS,
    TIMING_NOTES,
    clock,
    render_row,
)

settings = Settings.from_env()

chrome.heading("Edit Short")
st.caption(
    "Trim a suggested moment, frame it for 9:16, check the captions, then preview and export an "
    "MP4. Everything runs on this computer. Your original video and the transcript are never "
    "changed; edits are saved as settings for this Short only. Nothing is uploaded."
)


def _service(conn: object) -> ShortService:
    return build_short_service(settings, conn)  # type: ignore[arg-type]


def _flash(kind: str, text: str) -> None:
    st.session_state.setdefault("flash-editor", []).append((kind, text))


def _show_flash() -> None:
    for kind, text in st.session_state.pop("flash-editor", []):
        {"success": st.success, "error": st.error}.get(kind, st.info)(text)


# ----------------------------------------------------------------------------- selection

with closing(open_database(settings.db_path)) as conn:
    videos = [
        v
        for v in VideoRepository(conn).list_recent()
        if ClipRepository(conn).current(v.id) is not None
    ]
    analyses = ClipRepository(conn).current_by_video()
    candidates_by_video = {v.id: ClipRepository(conn).candidates(analyses[v.id].id) for v in videos}

if not videos:
    st.info(
        "No clip suggestions yet. Import a video, transcribe it, then press Find clips on the "
        "Clip Editor > Review candidates."
    )
    chrome.halt()

video_labels = {f"#{v.id}  {v.original_filename}": v for v in videos}
target = st.session_state.pop("editor-target", None)
if target is not None:
    wanted_video, wanted_key = target
    for label, v in video_labels.items():
        if v.id == wanted_video:
            st.session_state["editor-video"] = label
    for c in candidates_by_video.get(wanted_video, []):
        if c.key == wanted_key:
            st.session_state[f"editor-clip-{wanted_video}"] = f"#{c.rank}  {c.title}"
video = video_labels[st.selectbox("Video", list(video_labels), key="editor-video")]
candidates = candidates_by_video[video.id]
if not candidates:
    st.info("This video's analysis found no suggestions to edit.")
    chrome.halt()
clip_labels = {f"#{c.rank}  {c.title}": c for c in candidates}
clip = clip_labels[st.selectbox("Suggested clip", list(clip_labels), key=f"editor-clip-{video.id}")]

try:
    with closing(open_database(settings.db_path)) as conn:
        service = _service(conn)
        project = service.open_project(video.id, clip.key)
        info = service.source_info(video.id)
except IftyEditsError as exc:
    st.error(str(exc))
    chrome.halt()

pid = project.id
_show_flash()


def k(name: str) -> str:
    """Widget key that belongs to this project only, so clips never share settings."""
    return f"ed{pid}-{name}"


def _anchor_of(ss: Any) -> tuple[Any, ...]:
    """What a moving path was made for: the crop slider and the clip range."""
    return (ss.get(k("crop")), ss.get(k("start")), ss.get(k("end")))


def active_path(ss: Any) -> tuple[tuple[float, float], ...]:
    """The moving crop path, or nothing once the crop or range was changed by hand."""
    path = ss.get(k("path")) or []
    if not path or ss.get(k("path-anchor")) != _anchor_of(ss):
        return ()
    return tuple((float(t), float(p)) for t, p in path)


def load_widgets(config: ShortConfig) -> None:
    """Copy stored settings into this project's widget state."""
    st.session_state[k("start")] = round(config.start_s, 1)
    st.session_state[k("end")] = round(config.end_s, 1)
    st.session_state[k("crop")] = round(config.crop_position * 100)
    st.session_state[k("path")] = [list(p) for p in config.crop_path]
    st.session_state[k("path-anchor")] = _anchor_of(st.session_state)
    c = config.captions
    st.session_state[k("cap-on")] = c.enabled
    st.session_state[k("preset")] = c.preset if c.preset in CAPTION_PRESETS else "classic"
    st.session_state[k("font")] = c.font_size
    st.session_state[k("tcolor")] = c.text_color
    st.session_state[k("ocolor")] = c.outline_color
    st.session_state[k("outline")] = c.outline_px
    st.session_state[k("bg")] = c.background
    st.session_state[k("hl")] = c.highlight
    st.session_state[k("hcolor")] = c.highlight_color
    st.session_state[k("bgop")] = c.background_opacity
    st.session_state[k("emph")] = c.emphasis
    st.session_state[k("ecolor")] = c.emphasis_color
    st.session_state[k("bold")] = c.bold
    st.session_state[k("align")] = c.align
    st.session_state[k("pos")] = round(c.position, 2)
    st.session_state[k("width")] = round(c.width_fraction, 2)
    st.session_state[k("lines")] = c.max_lines
    a, p = config.audio, config.polish
    st.session_state[k("keep")] = a.keep
    st.session_state[k("vol")] = a.volume_percent
    st.session_state[k("afi")] = a.fade_in_s
    st.session_state[k("afo")] = a.fade_out_s
    st.session_state[k("norm")] = a.normalize
    st.session_state[k("vfi")] = p.fade_in_s
    st.session_state[k("vfo")] = p.fade_out_s
    st.session_state[k("bright")] = p.brightness
    st.session_state[k("contrast")] = p.contrast
    st.session_state[k("sat")] = p.saturation
    st.session_state[k("size")] = config.output.size
    st.session_state[k("quality")] = config.output.quality
    for key in [x for x in st.session_state if str(x).startswith(k("text-"))]:
        del st.session_state[key]
    st.session_state[k("overrides")] = dict(config.text_overrides)


if k("ready") not in st.session_state:
    load_widgets(project.config)
    st.session_state[k("ready")] = True


def on_preset() -> None:
    """Apply a caption preset's look to the style widgets."""
    values = CAPTION_PRESETS[st.session_state[k("preset")]]
    mapping = {
        "font_size": "font",
        "text_color": "tcolor",
        "outline_color": "ocolor",
        "outline_px": "outline",
        "background": "bg",
        "highlight": "hl",
        "highlight_color": "hcolor",
        "background_opacity": "bgop",
        "emphasis": "emph",
        "emphasis_color": "ecolor",
        "bold": "bold",
        "position": "pos",
        "width_fraction": "width",
        "max_lines": "lines",
    }
    for field, key in mapping.items():
        if field in values:
            st.session_state[k(key)] = values[field]


def on_reset_range() -> None:
    """Back to the AI-suggested start and end."""
    st.session_state[k("start")] = round(project.original_start_s, 1)
    st.session_state[k("end")] = round(project.original_end_s, 1)


def on_reset_frame() -> None:
    """Centre the crop (and drop any moving path)."""
    st.session_state[k("crop")] = 50
    st.session_state[k("path")] = []


def on_reset_captions() -> None:
    """Default caption look and no corrected text."""
    defaults = replace(project.config, captions=CaptionStyle(), text_overrides={})
    load_widgets(defaults)


def on_reset_all() -> None:
    """Everything back to the AI suggestion and default settings."""
    load_widgets(
        ShortConfig(
            start_s=round(project.original_start_s, 1),
            end_s=round(project.original_end_s, 1),
        )
    )


# ----------------------------------------------------------------------------- header

h1, h2, h3, h4 = st.columns(4)
h1.markdown(f"**AI suggestion**  \n{span_label(clip)}")  # a metric would ellipsize the range
h2.metric("Score", f"{round(clip.score)}")
h3.metric("Type", category_label(clip.category))
h4.metric("Source", f"{info.width}x{info.height}" + ("" if info.has_audio else ", no audio"))
if not info.has_audio:
    st.caption("This video has no audio track, so the Short will be silent.")

left, right = st.columns([3, 2], gap="large")

# ----------------------------------------------------------------------------- controls

with left:
    t_trim, t_frame, t_caps, t_look, t_out = st.tabs(
        ["Trim", "Framing", "Captions", "Look and sound", "Output"],
        key="editor-tab",
        on_change="rerun",
    )

    with t_trim:
        c1, c2 = st.columns(2)
        c1.number_input(
            "Start (seconds)", 0.0, float(info.duration_s), step=0.1, format="%.1f", key=k("start")
        )
        c2.number_input(
            "End (seconds)", 0.0, float(info.duration_s), step=0.1, format="%.1f", key=k("end")
        )
        length = st.session_state.get(k("end"), 0.0) - st.session_state.get(k("start"), 0.0)
        st.caption(
            f"Length {length:.1f}s (allowed {MIN_CLIP_S:.0f} to {MAX_CLIP_S:.0f}s). "
            f"Video is {clock(info.duration_s)} long. AI suggestion: "
            f"{clock(project.original_start_s)} to {clock(project.original_end_s)}."
        )
        st.button("Reset to the AI suggestion", on_click=on_reset_range, key=k("rr"))

    with t_frame:
        geometry = compute_crop(info.width, info.height, st.session_state.get(k("crop"), 50) / 100)
        if geometry.is_full_frame:
            st.info("This video is already 9:16, so nothing is cropped.")
        else:
            mode = "Auto-follow (the window moves slowly with the picture)"
            if not active_path(st.session_state):
                mode = "Manual or steady (one fixed window)"
            st.caption(f"Framing mode: {mode}")
            if settings.face_framing and load_face_detector(settings) is None:
                st.caption(
                    "Face framing is off because its model is not installed. "
                    "Run `uv run iftyedits models download yunet` (227 KB)."
                )
            if st.button(
                "Auto-frame",
                key=k("af"),
                help="Looks at frames across the saved clip range. With the face model "
                "installed the crop follows faces; otherwise it moves over the busiest part "
                "of the picture. It never identifies anyone. Moving the slider afterwards "
                "switches back to manual.",
            ):
                with closing(open_database(settings.db_path)) as conn:
                    try:
                        found = _service(conn).suggest_path(pid)
                    except IftyEditsError as exc:
                        st.error(str(exc))
                    else:
                        if found.method in ("follow", "steady"):
                            st.session_state[k("crop")] = round(found.keyframes[0][1] * 100)
                            st.session_state[k("path")] = (
                                [list(p) for p in found.keyframes] if found.moves else []
                            )
                            st.session_state[k("path-anchor")] = _anchor_of(st.session_state)
                        st.info(found.note)
            st.slider(
                "Crop position (0 = far left, 50 = center, 100 = far right)",
                0,
                100,
                key=k("crop"),
            )
            st.caption(
                f"Keeps a {geometry.width}x{geometry.height} part of the picture, "
                f"scaled to {geometry.out_w}x{geometry.out_h}. Nothing is stretched."
            )
        st.button("Center the crop", on_click=on_reset_frame, key=k("rf"))

    with t_caps:
        st.checkbox("Show captions", key=k("cap-on"))
        st.selectbox(
            "Style preset",
            list(CAPTION_PRESETS),
            format_func=lambda n: CAPTION_PRESET_LABELS.get(n, n),
            key=k("preset"),
            on_change=on_preset,
        )
        s1, s2 = st.columns(2)
        s1.slider("Text size", 28, 140, key=k("font"))
        s2.slider("Outline", 0, 20, key=k("outline"))
        s3, s4, s5 = st.columns(3)
        s3.color_picker("Text color", key=k("tcolor"))
        s4.color_picker("Outline color", key=k("ocolor"))
        s5.color_picker("Active word", key=k("hcolor"))
        e1, e2, e3 = st.columns([2, 1, 1])
        e1.checkbox(
            "Colour the key words",
            key=k("emph"),
            help="Colours a few important words in each caption: numbers, SHOUTED words and long "
            "content words. Common words are never coloured.",
        )
        e2.color_picker("Key word color", key=k("ecolor"))
        e3.checkbox("Bold text", key=k("bold"))
        s6, s7 = st.columns(2)
        s6.selectbox(
            "Background",
            ["none", "box"],
            format_func=lambda v: "None" if v == "none" else "Dark box",
            key=k("bg"),
        )
        s7.selectbox("Alignment", ["left", "center", "right"], key=k("align"))
        st.slider(
            "Vertical position (keeps clear of the app buttons)",
            float(SAFE_TOP),
            float(SAFE_BOTTOM),
            step=0.01,
            key=k("pos"),
        )
        s8, s9 = st.columns(2)
        s8.slider("Line width", 0.3, 0.88, step=0.01, key=k("width"))
        s9.slider("Most lines", 1, 4, key=k("lines"))
        st.checkbox(
            "Highlight the word being spoken",
            key=k("hl"),
            help="Only works when the transcript has per-word timing.",
        )

    with t_look:
        st.markdown("**Picture**")
        p1, p2 = st.columns(2)
        p1.slider("Fade in (s)", 0.0, 3.0, step=0.1, key=k("vfi"))
        p2.slider("Fade out (s)", 0.0, 3.0, step=0.1, key=k("vfo"))
        p3, p4, p5 = st.columns(3)
        p3.slider("Brightness", -0.3, 0.3, step=0.01, key=k("bright"))
        p4.slider("Contrast", 0.7, 1.4, step=0.01, key=k("contrast"))
        p5.slider("Saturation", 0.0, 2.0, step=0.05, key=k("sat"))
        st.markdown("**Sound**")
        if info.has_audio:
            st.checkbox("Keep the original audio", key=k("keep"))
            a1, a2, a3 = st.columns(3)
            a1.slider("Volume (%)", 0, 200, key=k("vol"))
            a2.slider("Sound fade in (s)", 0.0, 5.0, step=0.1, key=k("afi"))
            a3.slider("Sound fade out (s)", 0.0, 5.0, step=0.1, key=k("afo"))
            st.checkbox(
                "Even out loudness (about -16 LUFS)",
                key=k("norm"),
                help="Single-pass loudness normalisation. Leave off to keep the original level.",
            )
        else:
            st.caption("No audio track in this video.")

    with t_out:
        o1, o2 = st.columns(2)
        o1.selectbox("Size", list(SIZE_LABELS), format_func=SIZE_LABELS.__getitem__, key=k("size"))
        o2.selectbox(
            "Quality",
            list(QUALITY_LABELS),
            format_func=QUALITY_LABELS.__getitem__,
            key=k("quality"),
        )
        st.caption(
            "H.264 video and AAC audio in an MP4, 30 fps (60 for 60 fps sources). Sources "
            "smaller than the chosen size are scaled up; no new detail is created."
        )


def collect() -> ShortConfig:
    """Settings as currently shown in the widgets (not yet validated).

    Streamlit forgets the state of widgets that were not drawn in a run (for example the
    audio controls of a silent video), so every read has a default.
    """
    ss = st.session_state

    def g(name: str, default: Any) -> Any:
        return ss.get(k(name), default)

    overrides: dict[int, str] = dict(g("overrides", {}))
    originals: dict[int, str] = g("orig", {})
    prefix = k("text-")
    for key in [x for x in ss if isinstance(x, str) and x.startswith(prefix)]:
        position = int(key[len(prefix) :])
        text = clean_text(str(ss[key]))
        if text and text != originals.get(position, text):
            overrides[position] = text
        else:
            overrides.pop(position, None)
    ss[k("overrides")] = overrides
    return ShortConfig(
        start_s=float(g("start", project.config.start_s)),
        end_s=float(g("end", project.config.end_s)),
        crop_position=g("crop", 50) / 100,
        crop_path=active_path(ss),
        captions=CaptionStyle(
            enabled=bool(g("cap-on", True)),
            preset=g("preset", "classic"),
            font_size=int(g("font", 64)),
            text_color=g("tcolor", "#FFFFFF"),
            outline_color=g("ocolor", "#000000"),
            outline_px=int(g("outline", 6)),
            background=g("bg", "none"),
            background_opacity=float(g("bgop", 0.72)),
            highlight=bool(g("hl", False)),
            highlight_color=g("hcolor", "#FFD400"),
            emphasis=bool(g("emph", False)),
            emphasis_color=g("ecolor", "#FFD400"),
            bold=bool(g("bold", True)),
            align=g("align", "center"),
            position=float(g("pos", 0.68)),
            width_fraction=float(g("width", 0.86)),
            max_lines=int(g("lines", 2)),
        ),
        text_overrides=overrides,
        audio=AudioSettings(
            keep=bool(g("keep", True)),
            volume_percent=int(g("vol", 100)),
            fade_in_s=float(g("afi", 0.0)),
            fade_out_s=float(g("afo", 0.0)),
            normalize=bool(g("norm", False)),
        ),
        polish=PolishSettings(
            fade_in_s=float(g("vfi", 0.0)),
            fade_out_s=float(g("vfo", 0.0)),
            brightness=float(g("bright", 0.0)),
            contrast=float(g("contrast", 1.0)),
            saturation=float(g("sat", 1.0)),
        ),
        output=OutputSettings(size=g("size", "1080p"), quality=g("quality", "high")),
    )


try:
    config = collect().validate(info.duration_s)
    problem = ""
except ShortConfigError as exc:
    config = project.config
    problem = str(exc)

# ----------------------------------------------------------------------------- captions text

cues: list[CaptionCue] = []
segments = []
if not problem:
    try:
        with closing(open_database(settings.db_path)) as conn:
            service = _service(conn)
            segments = service.segments(video.id, config.start_s, config.end_s)
            cues = service.cues(video.id, config)
    except IftyEditsError as exc:
        problem = str(exc)

with left, st.expander(f"Caption text ({len(segments)} lines from the transcript)"):
    if not segments:
        st.caption("The transcript has no speech inside this range.")
    originals = st.session_state.setdefault(k("orig"), {})
    for segment in segments:
        original = clean_text(segment.text)
        originals[segment.position] = original
        key = k(f"text-{segment.position}")
        if key not in st.session_state:
            st.session_state[key] = st.session_state[k("overrides")].get(segment.position, original)
        st.text_input(clock(segment.start_s), key=key)
    st.caption("Corrections are saved with this Short only. The transcript itself is not changed.")
    st.button("Reset captions to defaults", on_click=on_reset_captions, key=k("rc"))

# ----------------------------------------------------------------------------- preview

with right:
    st.markdown("**Preview frame**")
    if problem:
        st.error(problem)
    else:
        at = st.slider(
            "Moment in the clip (s)",
            0.0,
            float(round(config.duration_s, 1)),
            min(1.5, config.duration_s / 2),
            step=0.5,
            key=k("at"),
        )
        try:
            with closing(open_database(settings.db_path)) as conn:
                still_path = _service(conn).still(video.id, config.start_s + at)
            frame = Image.open(still_path)
            geometry = compute_crop(
                info.width,
                info.height,
                position_at(config.crop_path, at) if config.crop_path else config.crop_position,
            )
            active = next((c for c in cues if c.start_s <= at < c.end_s), None)
            if active is None and cues:
                active = min(cues, key=lambda c: abs(c.start_s - at))
            word = None
            if active is not None and config.captions.highlight and active.can_highlight:
                word = next(
                    (i for i, w in enumerate(active.words) if w.start_s <= at < w.end_s), None
                )
            shot = framed_still(frame, geometry, active, config.captions, active_word=word)
            p_a, p_b = st.columns(2)
            p_a.image(crop_guide(frame, geometry), caption="Source with crop window")
            p_b.image(shot, caption="Short at this moment")
            if cues:
                st.caption(
                    f"{len(cues)} caption cues. "
                    + TIMING_NOTES.get(cues[0].timing, "")
                    + (
                        " Highlight needs word timing."
                        if config.captions.highlight and not any(c.can_highlight for c in cues)
                        else ""
                    )
                )
            elif config.captions.enabled:
                st.caption("No speech in this range, so there are no captions.")
        except IftyEditsError as exc:
            st.error(str(exc))

# ----------------------------------------------------------------------------- actions

st.divider()
a1, a2, a3, a4 = st.columns(4)


def _persist_and_request(kind: str, force: bool = False) -> None:
    try:
        with closing(open_database(settings.db_path)) as conn:
            service = _service(conn)
            service.save_config(pid, config)
            result = service.request_render(pid, kind, force=force)
    except IftyEditsError as exc:
        _flash("error", str(exc))
        return
    label = "export" if kind == KIND_EXPORT else "preview"
    if result.created:
        _flash("success", f"Queued {label} {result.render.id}. A worker must be running.")
    else:
        _flash("info", f"{result.reason}.")


if a1.button("Save settings", key=k("save"), disabled=bool(problem)):
    try:
        with closing(open_database(settings.db_path)) as conn:
            _service(conn).save_config(pid, config)
        _flash("success", "Settings saved for this Short.")
    except IftyEditsError as exc:
        _flash("error", str(exc))
    st.rerun()
if a2.button("Render preview video", key=k("prev"), disabled=bool(problem)):
    _persist_and_request(KIND_PREVIEW)
    st.rerun()
if a3.button("Export Short", key=k("exp"), type="primary", disabled=bool(problem)):
    _persist_and_request(KIND_EXPORT)
    st.rerun()
a4.button("Reset everything", on_click=on_reset_all, key=k("ra"))
st.caption(
    "A preview is a small, fast version (540x960) of the same crop, captions and sound. "
    "Export makes the finished file in the folder shown below and never overwrites an earlier one."
)


@st.fragment(run_every=2)
def render_status(project_id: int, current: ShortProject, source: SourceInfo) -> None:
    """Progress, results and errors of this Short's renders; refreshed every two seconds."""
    with closing(open_database(settings.db_path)) as conn:
        service = _service(conn)
        service.sync_renders(project_id)
        renders = ShortRepository(conn).list_renders(project_id)
        queue = JobQueue(conn)
        jobs = {r.id: queue.get(r.job_id) for r in renders if r.job_id is not None}
        workers = queue.live_workers()
    if not renders:
        st.caption("No previews or exports yet.")
        return
    for render in renders:
        if not render.active:
            continue
        job = jobs.get(render.id)
        kind = "Export" if render.kind == KIND_EXPORT else "Preview"
        with st.container(border=True):
            fraction = job.progress if job else 0.0
            note = job.message if job and job.message else "Waiting for a worker"
            st.progress(min(1.0, max(0.0, fraction)), text=f"{kind} {render.id}: {note}")
            if not workers and (job is None or job.status is JobStatus.QUEUED):
                st.warning("No worker is running. Start `uv run iftyedits worker`.")
            if st.button("Cancel", key=f"cancel-render-{render.id}"):
                with closing(open_database(settings.db_path)) as conn:
                    stopped = _service(conn).cancel_render(render.id)
                st.info("Cancel requested." if stopped else "That render already finished.")
    for kind_name, heading in ((KIND_EXPORT, "Latest export"), (KIND_PREVIEW, "Latest preview")):
        done = next(
            (
                r
                for r in renders
                if r.kind == kind_name
                and r.status == "succeeded"
                and r.output_path is not None
                and r.output_path.is_file()
            ),
            None,
        )
        failed = next((r for r in renders if r.kind == kind_name and r.status == "failed"), None)
        if failed is not None and (done is None or failed.id > done.id):
            st.error(f"{heading} {failed.id} failed: {failed.error}")
        if done is not None and done.output_path is not None:
            st.markdown(f"**{heading}** (render {done.id})")
            width = done.width or 0
            st.caption(
                f"`{done.output_path}`  \n{width}x{done.height}, {done.duration_s or 0:.1f}s, "
                f"{human_size(done.size_bytes or 0)}, "
                f"{'with audio' if done.has_audio else 'no audio'}, encoder {done.encoder}"
            )
            _, middle, _ = st.columns([1, 1, 1])  # a 9:16 video at full width is too tall
            middle.video(str(done.output_path))
    with st.expander("All renders of this Short"):
        st.dataframe([render_row(r) for r in renders], hide_index=True, width="stretch")
    del current, source


render_status(pid, project, info)
