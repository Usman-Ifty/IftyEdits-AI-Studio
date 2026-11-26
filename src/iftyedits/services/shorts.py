"""Short editing and rendering: projects, captions, preview and export.

A *project* is the editable record for one suggested clip. Editing changes the project's
settings only; the Phase 2 suggestion, the transcript and the original video are read, never
written. Preview and export are queued as background jobs and run by the worker through
:meth:`ShortService.run_render`:

1. check the original is in the vault and the settings are valid for this video;
2. draw the captions to PNG frames in a work folder;
3. render with FFmpeg to a ``.partial`` file (never the final name);
4. probe the file and decode it fully; only a verified file is kept;
5. move it to its final, unique name without replacing anything, then record the result.

Any failure removes the partial file and records the error; earlier exports are untouched.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image

from iftyedits.adapters.ffmpeg import SOFTWARE_H264, FFmpeg
from iftyedits.adapters.framing import column_interest, picture_change
from iftyedits.adapters.short_render import (
    CaptionAssets,
    EncodeChoice,
    VerifiedOutput,
    build_args,
    extract_frame,
    pick_encoder,
    verify_output,
    write_caption_assets,
)
from iftyedits.config import Settings
from iftyedits.core.captions import CaptionCue, build_cues
from iftyedits.core.clip_models import SignalKind
from iftyedits.core.errors import (
    ClipNotFoundError,
    ExternalToolError,
    FaceDetectionError,
    IftyEditsError,
    InsufficientSpaceError,
    InvalidSourceError,
    JobCancelledError,
    ShortConfigError,
    ShortRenderError,
)
from iftyedits.core.framing import (
    CENTER,
    DEAD_ZONE,
    CropPath,
    CropSuggestion,
    FramingObservation,
    best_window,
    face_position,
    follow_subject,
    suggest_position,
)
from iftyedits.core.models import JobStatus, StoredSegment, Video
from iftyedits.core.protocols import CancelToken, FaceDetector, MediaProber, MediaTranscoder
from iftyedits.core.shorts import ShortConfig, compute_crop
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.originals import assert_safe_output_path
from iftyedits.storage.shorts import (
    KIND_EXPORT,
    KIND_PREVIEW,
    ShortProject,
    ShortRender,
    ShortRepository,
)
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
TranscoderFactory = Callable[[], MediaTranscoder]
SHORTS_DIR = "shorts"
PREVIEW_SIZE = "540p"
ENCODE_SHARE = 0.92
"""Share of the progress bar used by the FFmpeg encode (the rest is checks and moving)."""
CUT_CHANGE = 0.18
MIN_PATH_WEIGHT = 0.05
FACE_STEP_S = 1.0
"""Seconds between pictures checked for faces inside one shot."""
FACE_MAX_SAMPLES = 60
SHOT_MARGIN_S = 0.1
"""A shot is first sampled this long after its cut, past any blended frame."""
MIN_FACE_COVERAGE = 0.25
"""Below this share of pictures with a face, the clip is framed by picture detail instead."""


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """What the render needs to know about the original, after rotation is applied."""

    width: int
    height: int
    fps: float | None
    has_audio: bool
    duration_s: float


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Outcome of asking for a preview or export."""

    render: ShortRender
    created: bool
    """False when an identical active or finished render was reused."""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RenderReport:
    """What a finished render produced."""

    render_id: int
    output_path: Path
    verified: VerifiedOutput
    encoder: str
    captions: int

    def as_result(self) -> dict[str, Any]:
        """Stored on the job."""
        return {
            "render_id": self.render_id,
            "output": str(self.output_path),
            "width": self.verified.width,
            "height": self.verified.height,
            "duration_s": round(self.verified.duration_s, 3),
            "size_bytes": self.verified.size_bytes,
            "has_audio": self.verified.has_audio,
            "encoder": self.encoder,
        }

    def summary(self) -> str:
        """One line for the jobs list."""
        v = self.verified
        return f"{self.output_path.name}: {v.width}x{v.height}, {v.duration_s:.1f}s, {self.encoder}"


def slugify(text: str, *, limit: int = 40) -> str:
    """Safe, readable file-name part: lowercase ASCII letters, digits and single hyphens."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:limit].strip("-")
    return slug or "short"


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m{whole % 60:02d}s"


def export_stem(video: Video, config: ShortConfig, config_hash: str) -> str:
    """File name (without extension) that identifies the source, the range and the settings."""
    stem = Path(video.original_filename).stem
    return (
        f"{slugify(stem)}_v{video.id}_{_clock(config.start_s)}-{_clock(config.end_s)}"
        f"_{config_hash[:8]}"
    )


def source_folder(video: Video) -> str:
    """Per-source export folder name, so each video's Shorts stay together."""
    return f"{slugify(Path(video.original_filename).stem)}-v{video.id}"


def claim_unique_path(partial: Path, directory: Path, stem: str, suffix: str = ".mp4") -> Path:
    """Give ``partial`` a final name that did not exist, without ever replacing a file.

    Uses a hard link (which fails if the target exists) and then removes the partial name, so
    two renders racing for one name cannot overwrite each other.
    """
    for number in range(1, 1000):
        name = f"{stem}{suffix}" if number == 1 else f"{stem}-{number}{suffix}"
        target = directory / name
        try:
            os.link(partial, target)
        except FileExistsError:
            continue
        partial.unlink()
        return target
    raise ShortRenderError("Could not find a free export file name")


def shot_samples(
    cuts: Sequence[float], duration_s: float, *, max_samples: int = FACE_MAX_SAMPLES
) -> list[tuple[float, float, bool]]:
    """``(reading time, picture time, starts a shot)`` for every picture to check.

    Each shot is sampled just after its cut and then every :data:`FACE_STEP_S`. The first
    reading of a shot is dated at the cut itself, so the window moves exactly there. Long
    clips are sampled more coarsely to stay within ``max_samples``.
    """
    edges = [0.0, *sorted(c for c in cuts if 0.0 < c < duration_s), duration_s]
    step = max(FACE_STEP_S, duration_s / max_samples)
    out: list[tuple[float, float, bool]] = []
    for begin, end in pairwise(edges):
        at = begin + min(SHOT_MARGIN_S, (end - begin) / 2)
        first = True
        while first or at < end - SHOT_MARGIN_S:
            out.append((begin if first else at, at, first))
            first = False
            at += step
    if len(out) > max_samples:
        keep = max(1.0, len(out) / max_samples)
        out = [out[int(i * keep)] for i in range(max_samples)]
    return out


def build_short_service(settings: Settings, conn: sqlite3.Connection) -> ShortService:
    """A service wired to the real FFmpeg and ffprobe, for the editor page and the CLI."""
    from iftyedits.adapters.faces import load_face_detector
    from iftyedits.adapters.ffprobe import DeferredFFprobe

    return ShortService(
        settings,
        VideoRepository(conn),
        TranscriptRepository(conn),
        ClipRepository(conn),
        ShortRepository(conn),
        JobQueue(conn),
        DeferredFFprobe(settings.ffprobe_path, settings.probe_timeout_s),
        lambda: FFmpeg(
            settings.ffmpeg_path, settings.ffmpeg_timeout_s, encoder=settings.export_encoder
        ),
        load_face_detector(settings),
    )


class ShortService:
    """Use cases for the editor and the worker."""

    def __init__(
        self,
        settings: Settings,
        videos: VideoRepository,
        transcripts: TranscriptRepository,
        clips: ClipRepository,
        shorts: ShortRepository,
        queue: JobQueue,
        prober: MediaProber,
        transcoder: TranscoderFactory,
        faces: FaceDetector | None = None,
    ) -> None:
        self.settings = settings
        self.videos = videos
        self.transcripts = transcripts
        self.clips = clips
        self.shorts = shorts
        self.queue = queue
        self.prober = prober
        self.transcoder = transcoder
        self.faces = faces

    # ------------------------------------------------------------------ paths

    @property
    def exports_dir(self) -> Path:
        """Where finished Shorts are saved."""
        return self.settings.exports_dir / SHORTS_DIR

    def _safe(self, path: Path) -> Path:
        return assert_safe_output_path(
            path,
            originals_root=self.settings.originals_dir,
            allowed_roots=self.settings.output_roots,
        )

    def original_path(self, video: Video) -> Path:
        """The vault file, checked to exist. It is only ever read."""
        path = self.settings.originals_dir / video.vault_relpath
        if not path.is_file():
            raise InvalidSourceError(
                f"The original video is missing from the vault ({video.vault_relpath}). "
                "Run `uv run iftyedits reconcile` and check the vault."
            )
        return path

    # ------------------------------------------------------------------ source

    def source_info(self, video_id: int) -> SourceInfo:
        """Display size, frame rate and audio presence from the stored probe."""
        video = self._video(video_id)
        streams = self.videos.streams(video_id)
        picture = next(
            (s for s in streams if s.codec_type == "video" and not s.is_attached_pic), None
        )
        size = picture.display_size if picture else None
        if picture is None or size is None:
            raise InvalidSourceError("This video has no usable picture stream")
        if video.duration_s is None or video.duration_s <= 0:
            raise InvalidSourceError("This video's length is unknown, so it cannot be edited")
        return SourceInfo(
            width=size[0],
            height=size[1],
            fps=picture.avg_frame_rate or picture.r_frame_rate,
            has_audio=any(s.codec_type == "audio" for s in streams),
            duration_s=video.duration_s,
        )

    def _video(self, video_id: int) -> Video:
        video = self.videos.get(video_id)
        if video is None:
            raise InvalidSourceError(f"Video {video_id} does not exist")
        return video

    # ------------------------------------------------------------------ projects

    def open_project(self, video_id: int, candidate_key: str) -> ShortProject:
        """The editable Short for a suggestion; created from the suggestion on first use."""
        existing = self.shorts.find_project(video_id, candidate_key)
        if existing is not None:
            return existing
        candidate = self.clips.candidate_by_key(video_id, candidate_key)
        if candidate is None:
            raise ClipNotFoundError("That clip suggestion no longer exists")
        info = self.source_info(video_id)
        config = ShortConfig(
            start_s=candidate.effective_start_s,
            end_s=min(candidate.effective_end_s, info.duration_s),
        ).validate(info.duration_s)
        return self.shorts.create_project(
            video_id, candidate_key, candidate.title, candidate.start_s, candidate.end_s, config
        )

    def project(self, project_id: int) -> ShortProject:
        """A project by id."""
        project = self.shorts.get_project(project_id)
        if project is None:
            raise ShortConfigError("That Short project no longer exists")
        return project

    def save_config(self, project_id: int, config: ShortConfig) -> ShortConfig:
        """Validate against the video and store. Returns the validated settings."""
        project = self.project(project_id)
        checked = config.validate(self.source_info(project.video_id).duration_s)
        self.shorts.save_config(project_id, checked)
        return checked

    def reset_range(self, project_id: int) -> ShortConfig:
        """Put the start and end back to the original AI suggestion; keep other settings."""
        project = self.project(project_id)
        from dataclasses import replace

        config = replace(
            project.config, start_s=project.original_start_s, end_s=project.original_end_s
        )
        return self.save_config(project_id, config)

    # ------------------------------------------------------------------ captions

    def segments(self, video_id: int, start_s: float, end_s: float) -> list[StoredSegment]:
        """Current transcript segments overlapping a range (empty if never transcribed)."""
        transcript = self.transcripts.current(video_id)
        if transcript is None:
            return []
        return [
            s
            for s in self.transcripts.segments(transcript.id)
            if s.end_s > start_s and s.start_s < end_s
        ]

    def cues(self, video_id: int, config: ShortConfig) -> list[CaptionCue]:
        """Caption cues for these settings, in clip time."""
        segments = self.segments(video_id, config.start_s, config.end_s)
        return build_cues(
            segments, config.start_s, config.end_s, config.captions, config.text_overrides
        )

    # ------------------------------------------------------------------ stills

    def still(self, video_id: int, at_s: float) -> Path:
        """A cached PNG of the source at ``at_s`` (for the crop guide and caption preview)."""
        video = self._video(video_id)
        source = self.original_path(video)
        folder = self._safe(self.settings.derived_dir / SHORTS_DIR / "stills")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"v{video.id}_{round(at_s * 1000):09d}.png"
        if not target.is_file():
            partial = folder / f".{target.name}.partial.png"
            extract_frame(self.transcoder(), source, at_s, self._safe(partial))
            partial.replace(target)
        return target

    def suggest_crop(self, project_id: int, samples: int = 5) -> CropSuggestion:
        """Where the vertical window should sit over this clip (never raises for bad frames).

        Samples a few frames across the clip, judges each by visual detail and returns one
        steady position. Falls back to the centre when frames cannot be read or nothing
        stands out. The caller decides whether to apply it; nothing is saved here.
        """
        project = self.project(project_id)
        config = project.config
        info = self.source_info(project.video_id)
        crop = compute_crop(info.width, info.height, 0.5)
        if crop.width >= info.width - 2:
            return CropSuggestion(0.5, "none", 0.0, "The picture is not wider than 9:16.")
        step = config.duration_s / (samples + 1)
        frames: list[list[float]] = []
        for index in range(samples):
            at = config.start_s + step * (index + 1)
            try:
                with Image.open(self.still(project.video_id, at)) as image:
                    frames.append(column_interest(image))
            except (IftyEditsError, OSError) as exc:
                logger.info("Skipped a frame while framing: %s", exc)
        return suggest_position(frames, crop.width / info.width)

    def suggest_path(self, project_id: int, *, max_samples: int = 12) -> CropPath:
        """A calm moving window for this clip, smoothed by :func:`follow_subject`.

        With a face detector the window follows faces and jumps at scene cuts. Without one,
        or when the clip shows few faces, frames are sampled about every two seconds and
        judged by picture detail. Never raises for unreadable frames; the result may be
        steady or centred. Nothing is saved here and no identity is looked for or stored.
        """
        project = self.project(project_id)
        config = project.config
        info = self.source_info(project.video_id)
        crop = compute_crop(info.width, info.height, 0.5)
        if crop.width >= info.width - 2:
            return CropPath(((0.0, 0.5),), "none", 0.0, "The picture is not wider than 9:16.")
        window = crop.width / info.width
        followed = self._face_path(project.video_id, config, window)
        if followed is not None:
            return followed
        count = max(3, min(max_samples, round(config.duration_s / 2.0)))
        step = config.duration_s / count
        found: list[FramingObservation] = []
        previous: Image.Image | None = None
        for index in range(count):
            offset = step * (index + 0.5)
            try:
                with Image.open(self.still(project.video_id, config.start_s + offset)) as image:
                    image.load()
                    position, contrast = best_window(column_interest(image), window)
                    cut = previous is not None and picture_change(previous, image) > CUT_CHANGE
                    previous = image.copy()
            except (IftyEditsError, OSError) as exc:
                logger.info("Skipped a frame while framing: %s", exc)
                continue
            weight = max(0.0, min(1.0, (contrast - 1.0) / 2.0))
            found.append(FramingObservation(offset, position, weight, cut))
        if all(o.weight < MIN_PATH_WEIGHT for o in found):
            return CropPath(((0.0, 0.5),), "center", 0.0, "Nothing stood out, so it is centred.")
        return follow_subject(found, config.duration_s)

    def _cut_times(self, video_id: int, start_s: float, end_s: float) -> list[float]:
        """Scene cuts of the current analysis inside the clip, in clip time."""
        analysis = self.clips.current(video_id)
        if analysis is None:
            return []
        return sorted(
            s.start_s - start_s
            for s in self.clips.signals(analysis.id)
            if s.kind is SignalKind.SCENE_CUT
            and start_s + SHOT_MARGIN_S < s.start_s < end_s - 2 * SHOT_MARGIN_S
        )

    def _face_path(self, video_id: int, config: ShortConfig, window: float) -> CropPath | None:
        """A window path that follows faces, or ``None`` to use picture detail instead."""
        if self.faces is None:
            return None
        samples = shot_samples(
            self._cut_times(video_id, config.start_s, config.end_s), config.duration_s
        )
        observations: list[FramingObservation] = []
        previous: Image.Image | None = None
        position: float | None = None
        read = with_face = 0
        shot: list[tuple[FramingObservation, bool]] = []

        def close_shot() -> None:
            """Keep face readings; a shot with no face at all is framed by detail."""
            faces_seen = any(seen for _, seen in shot)
            kept = [o for o, seen in shot if seen == faces_seen]
            if kept and shot[0][0].cut:
                kept[0] = replace(kept[0], time_s=shot[0][0].time_s, cut=True)
            observations.extend(kept)
            shot.clear()

        for index, (time_s, at_s, new_shot) in enumerate(samples):
            try:
                still = self.still(video_id, config.start_s + at_s)
                with Image.open(still) as image:
                    image.load()
                    changed = previous is not None and picture_change(previous, image) > CUT_CHANGE
                    cut = new_shot or changed
                    if cut and index:
                        close_shot()
                        position = None
                    found = face_position(self.faces.detect(still), window, position)
                    if found is None:
                        fallback, contrast = best_window(column_interest(image), window)
                        weight = max(0.0, min(1.0, (contrast - 1.0) / 2.0))
                        if weight < MIN_PATH_WEIGHT:
                            fallback, weight = CENTER, MIN_PATH_WEIGHT
                    else:
                        position, weight = found
                        fallback = position
                        with_face += 1
                    previous = image.copy()
            except FaceDetectionError as exc:
                logger.warning("Face framing skipped: %s", exc)
                return None
            except (IftyEditsError, OSError) as exc:
                logger.info("Skipped a frame while framing: %s", exc)
                continue
            read += 1
            shot.append(
                (FramingObservation(time_s, fallback, weight, cut and bool(index)), bool(found))
            )
        if shot:
            close_shot()
        if not read or with_face / read < MIN_FACE_COVERAGE:
            return None
        path = follow_subject(observations, config.duration_s, cut_jump=DEAD_ZONE)
        if path.method not in ("follow", "steady"):
            return None
        note = (
            "The window follows faces, panning slowly and jumping at scene cuts."
            if path.method == "follow"
            else "The window is steady, placed on the faces."
        )
        return replace(path, note=f"{note} Faces were seen in {with_face} of {read} pictures.")

    # ------------------------------------------------------------------ requests

    def sync_renders(self, project_id: int) -> None:
        """Close renders whose job ended without the worker recording it (for example a
        job cancelled while still queued), so they do not look busy or block a new request."""
        for render in self.shorts.list_renders(project_id):
            if not render.active:
                continue
            job = self.queue.get(render.job_id) if render.job_id is not None else None
            if job is not None and job.status in (JobStatus.CANCELLED, JobStatus.FAILED):
                self.shorts.mark_failed(
                    render.id,
                    job.error or "Cancelled",
                    cancelled=job.status == JobStatus.CANCELLED,
                )

    def cancel_render(self, render_id: int) -> bool:
        """Ask for a queued or running render to stop. Returns False if it is not active."""
        render = self.shorts.get_render(render_id)
        if render is None or not render.active or render.job_id is None:
            return False
        self.queue.request_cancel(render.job_id)
        self.sync_renders(render.project_id)
        return True

    def request_render(
        self, project_id: int, kind: str, *, force: bool = False, queue_job: bool = True
    ) -> RenderRequest:
        """Queue a preview or export of the project's current settings.

        An identical request that is queued or running is reused (no duplicate work). An
        identical *finished* preview is reused too; an identical finished export is reused
        unless ``force`` asks for another copy, which gets its own file name.
        """
        if kind not in (KIND_PREVIEW, KIND_EXPORT):
            raise ShortConfigError(f"Unknown render kind {kind!r}")
        project = self.project(project_id)
        video = self._video(project.video_id)
        info = self.source_info(project.video_id)
        self.original_path(video)
        config = project.config.validate(info.duration_s)
        self.sync_renders(project_id)
        digest = config.config_hash(video_sha256=video.sha256, kind=kind, has_audio=info.has_audio)
        active = self.shorts.find_active(project_id, kind, digest)
        if active is not None:
            return RenderRequest(active, False, "The same request is already running")
        if not force:
            done = self.shorts.find_succeeded(project_id, kind, digest)
            if done is not None:
                return RenderRequest(done, False, "This exact Short was already rendered")
        try:
            render = self.shorts.create_render(project_id, kind, digest, config)
        except sqlite3.IntegrityError:
            raced = self.shorts.find_active(project_id, kind, digest)
            if raced is None:
                raise
            return RenderRequest(raced, False, "The same request is already running")
        if not queue_job:
            return RenderRequest(render, True)  # the caller runs it inline
        try:
            job_id, _ = self.queue.enqueue_short_render(render.id, project.video_id)
        except Exception as exc:
            self.shorts.mark_failed(render.id, f"Could not queue the job: {exc}")
            raise
        self.shorts.attach_job(render.id, job_id)
        refreshed = self.shorts.get_render(render.id) or render
        return RenderRequest(refreshed, True)

    # ------------------------------------------------------------------ worker

    def run_render(
        self,
        render_id: int,
        *,
        progress: Progress | None = None,
        cancel: CancelToken | None = None,
    ) -> RenderReport:
        """Do the work of one queued render. Records success or failure on the row."""
        render = self.shorts.get_render(render_id)
        if render is None:
            raise ShortRenderError("That render no longer exists")
        if render.status == "succeeded" and render.output_path and render.output_path.is_file():
            verified = VerifiedOutput(
                width=render.width or 0,
                height=render.height or 0,
                duration_s=render.duration_s or 0.0,
                size_bytes=render.size_bytes or 0,
                has_audio=bool(render.has_audio),
                video_codec="h264",
            )
            return RenderReport(render_id, render.output_path, verified, render.encoder or "", 0)
        self.shorts.mark_running(render_id)
        work = self.settings.derived_dir / SHORTS_DIR / "work" / f"render_{render_id}"
        try:
            report = self._render(render, work, progress or _ignore, cancel)
        except JobCancelledError as exc:
            self._discard(work)
            self.shorts.mark_failed(render_id, str(exc), cancelled=True)
            raise
        except Exception as exc:
            self._discard(work)
            self.shorts.mark_failed(render_id, _user_message(exc))
            raise
        self._discard(work)
        return report

    def _discard(self, work: Path) -> None:
        if work.is_dir():
            shutil.rmtree(self._safe(work), ignore_errors=True)

    def _render(
        self,
        render: ShortRender,
        work: Path,
        progress: Progress,
        cancel: CancelToken | None,
    ) -> RenderReport:
        project = self.project(render.project_id)
        video = self._video(project.video_id)
        source = self.original_path(video)
        info = self.source_info(video.id)
        config = render.config.validate(info.duration_s)
        preview = render.kind == KIND_PREVIEW
        size = PREVIEW_SIZE if preview else config.output.size
        crop = compute_crop(info.width, info.height, config.crop_position, out_size=size)
        progress(0.02, "Preparing captions")
        cues = self.cues(video.id, config)
        assets: CaptionAssets | None = write_caption_assets(
            cues, config.captions, (crop.out_w, crop.out_h), config.duration_s, self._safe(work)
        )
        directory = self._safe(
            self.settings.derived_dir / SHORTS_DIR / "previews"
            if preview
            else self.exports_dir / ".partial"
        )
        directory.mkdir(parents=True, exist_ok=True)
        self._check_space(directory)
        partial = self._safe(directory / f"render_{render.id}.partial")
        partial.unlink(missing_ok=True)
        transcoder = self.transcoder()
        caps = transcoder.capabilities()
        hardware = bool(caps.h264 and caps.h264.hardware)
        encoder = pick_encoder(self.settings.export_encoder, hardware)
        duration = config.duration_s

        def encode(choice: EncodeChoice) -> None:
            args = build_args(
                config,
                source=source,
                output=partial,
                crop=crop,
                source_fps=info.fps,
                has_audio=info.has_audio,
                captions=assets,
                encoder=choice,
                preview=preview,
                limiter="alimiter" in caps.filters,
            )

            def on_seconds(seconds: float) -> None:
                progress(0.05 + ENCODE_SHARE * min(seconds / duration, 1.0), "Rendering")

            transcoder.run(args, progress=on_seconds, cancel=cancel, redact=(source, partial))

        try:
            progress(0.05, "Rendering")
            try:
                encode(encoder)
            except ExternalToolError as exc:
                if not encoder.hardware:
                    raise
                logger.warning("Hardware encode failed (%s); retrying with libx264", exc)
                partial.unlink(missing_ok=True)
                encoder = EncodeChoice(SOFTWARE_H264, False)
                encode(encoder)
            progress(0.97, "Checking the finished file")
            verified = verify_output(
                partial,
                expect_size=(crop.out_w, crop.out_h),
                expect_duration_s=duration,
                expect_audio=info.has_audio and config.audio.keep,
                prober=self.prober,
                ffmpeg=transcoder,
            )
            if preview:
                final = self._safe(
                    directory / f"preview_p{project.id}_{render.config_hash[:12]}.mp4"
                )
                partial.replace(final)
            else:
                stem = export_stem(video, config, render.config_hash)
                folder = self._safe(directory.parent / source_folder(video))
                folder.mkdir(parents=True, exist_ok=True)
                final = claim_unique_path(partial, folder, stem)
            self._safe(final)
            self.shorts.mark_succeeded(
                render.id,
                output_path=final,
                width=verified.width,
                height=verified.height,
                duration_s=verified.duration_s,
                size_bytes=verified.size_bytes,
                has_audio=verified.has_audio,
                encoder=encoder.name,
            )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        progress(1.0, "Done")
        return RenderReport(render.id, final, verified, encoder.name, len(cues))

    def _check_space(self, directory: Path) -> None:
        free = shutil.disk_usage(directory).free
        if free < self.settings.min_free_bytes:
            raise InsufficientSpaceError(
                f"Only {free / 1024**3:.1f} GB is free; at least "
                f"{self.settings.min_free_bytes / 1024**3:.1f} GB is needed to export"
            )


def _ignore(_fraction: float, _message: str) -> None:
    return None


def _user_message(exc: Exception) -> str:
    """Short text for the render row; details stay in the log."""
    if isinstance(exc, ShortRenderError | ShortConfigError | InvalidSourceError):
        return str(exc)
    if isinstance(exc, ExternalToolError | InsufficientSpaceError):
        return str(exc)
    return f"Unexpected error: {type(exc).__name__}: {exc}"
