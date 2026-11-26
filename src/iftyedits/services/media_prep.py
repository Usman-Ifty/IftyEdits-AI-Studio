"""Media preparation: 16 kHz mono WAV, 720p H.264 proxy and a thumbnail per video.

For each requested stage:

1. Compute the stage parameters and their hash. The output path is deterministic:
   ``derived/<sha[:2]>/<sha>/<name>-<params_hash>.<ext>``.
2. If a registered artifact has the same parameters and its file still matches its
   size and SHA-256, reuse it (unless ``force``).
3. Otherwise FFmpeg writes a uniquely named ``*.partial`` file next to the final path.
   The output is probed and validated, hashed, atomically renamed into place and
   registered. Partial files are always removed on failure or cancellation.

The original is only ever read. Every output path passes ``assert_safe_output_path``.
A failed stage is recorded and the remaining stages still run; cancellation stops at
once. Previously completed artifacts are never removed by a failed or cancelled run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iftyedits.config import Settings
from iftyedits.core.errors import (
    ExternalToolError,
    IftyEditsError,
    InvalidSourceError,
    JobCancelledError,
    MediaValidationError,
    OriginalProtectionError,
    ProbeError,
    ProcessingError,
)
from iftyedits.core.models import (
    ALL_STAGES,
    STAGE_ARTIFACT,
    Artifact,
    EncoderInfo,
    MediaInfo,
    Stage,
    StageStatus,
    StreamInfo,
    Video,
    VideoStatus,
)
from iftyedits.core.protocols import CancelToken, MediaProber, MediaTranscoder
from iftyedits.storage.originals import assert_safe_output_path, is_within, sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

STAGE_VERSION = {Stage.AUDIO: 1, Stage.PROXY: 1, Stage.THUMBNAIL: 1}
AUDIO_SAMPLE_RATE = 16_000
PROXY_SHORT_SIDE = 720
PROXY_MAX_FPS = 30.0
PROXY_VT_BITRATE = "3M"
PROXY_X264_CRF = 26
THUMB_WIDTH, THUMB_HEIGHT = 640, 360
THUMB_SAMPLE_FRAMES = 50
PARTIAL_SUFFIX = ".partial"
STALE_PARTIAL_AGE_S = 3600.0
_PROGRESS_WRITE_INTERVAL_S = 1.0

OverallProgress = Callable[[float, str], None]
"""Called with overall progress (0.0 to 1.0) and a short status message."""


def params_hash(params: dict[str, Any]) -> str:
    """Stable 16-character hash of stage parameters."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def proxy_size(width: int, height: int, short_side: int = PROXY_SHORT_SIDE) -> tuple[int, int]:
    """Proxy frame size: shorter side at most ``short_side``, never upscaled, even numbers."""
    factor = min(1.0, short_side / min(width, height))
    return int(width * factor / 2) * 2, int(height * factor / 2) * 2


def proxy_scale_filter(short_side: int = PROXY_SHORT_SIDE) -> str:
    """FFmpeg filter chain matching :func:`proxy_size`, applied after auto-rotation."""
    factor = f"min(1,{short_side}/min(iw,ih))"
    return f"scale=w='trunc({factor}*iw/2)*2':h='trunc({factor}*ih/2)*2',setsar=1,format=yuv420p"


def proxy_fps(stream: StreamInfo) -> float:
    """Constant output frame rate: the source average rate, capped at 30 fps."""
    rate = stream.avg_frame_rate or stream.r_frame_rate
    if not rate or rate <= 0:
        return PROXY_MAX_FPS
    return round(min(rate, PROXY_MAX_FPS), 3)


def thumbnail_seek(duration_s: float | None) -> float:
    """Seek point for the thumbnail: 10% into the video, skipping intros and fades."""
    if duration_s is None or duration_s < 3.0:
        return 0.0
    return round(duration_s * 0.1, 3)


def thumbnail_filter() -> str:
    """Pick a representative (non-black) frame, fit it into a fixed 640x360 frame."""
    return (
        f"thumbnail={THUMB_SAMPLE_FRAMES},"
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,format=yuvj420p"
    )


def clean_partials(derived_dir: Path, max_age_s: float = STALE_PARTIAL_AGE_S) -> int:
    """Remove abandoned ``*.partial`` outputs older than ``max_age_s`` from derived media.

    Only regular files named ``*.partial`` inside ``derived_dir`` are touched.
    """
    if not derived_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for partial in derived_dir.rglob(f"*{PARTIAL_SUFFIX}"):
        if partial.is_symlink() or not partial.is_file() or partial.stat().st_mtime > cutoff:
            continue
        if not is_within(partial, derived_dir):
            continue
        partial.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.warning("Removed %d abandoned partial output(s)", removed)
    return removed


def durations_match(expected: float | None, actual: float | None) -> bool:
    """Output duration within max(1 s, 2%) of the source; unknown source passes."""
    if expected is None or expected <= 0:
        return True
    if actual is None:
        return False
    return abs(actual - expected) <= max(1.0, expected * 0.02)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What one stage did."""

    stage: Stage
    status: StageStatus
    message: str
    artifact: Artifact | None = None
    reused: bool = False
    error: str | None = None


@dataclass(slots=True)
class PrepareReport:
    """All stage outcomes for one video."""

    video_id: int
    outcomes: list[StageOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[StageOutcome]:
        """Stages that failed."""
        return [o for o in self.outcomes if o.status is StageStatus.FAILED]

    def as_result(self) -> dict[str, Any]:
        """JSON-serialisable job result."""
        return {
            "video_id": self.video_id,
            "stages": {o.stage.value: o.status.value for o in self.outcomes},
            "reused": [o.stage.value for o in self.outcomes if o.reused],
            "artifacts": {
                o.stage.value: o.artifact.relpath for o in self.outcomes if o.artifact is not None
            },
        }

    def summary(self) -> str:
        """One-line description for the job message."""
        return "; ".join(f"{o.stage.value}: {o.message}" for o in self.outcomes)


@dataclass(slots=True)
class _Context:
    video: Video
    source: Path
    primary: StreamInfo | None
    audio: StreamInfo | None
    job_id: int | None
    worker_id: str | None
    cancel: CancelToken | None
    report: Callable[[float, str], None]


class MediaPrepService:
    """Runs the audio, proxy and thumbnail stages for imported videos."""

    def __init__(
        self,
        settings: Settings,
        videos: VideoRepository,
        processing: ProcessingRepository,
        transcoder: MediaTranscoder,
        prober: MediaProber,
    ) -> None:
        self.settings = settings
        self.videos = videos
        self.processing = processing
        self.transcoder = transcoder
        self.prober = prober

    # ------------------------------------------------------------------ public

    def prepare(
        self,
        video_id: int,
        stages: Sequence[Stage] = ALL_STAGES,
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: OverallProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> PrepareReport:
        """Run ``stages`` (in pipeline order) for one video.

        Raises ``JobCancelledError`` when cancelled, ``JobOwnershipError`` if the job
        was taken away from this worker, and ``ProcessingError`` if any stage failed
        (after the other stages have run).
        """
        video = self.videos.get(video_id)
        if video is None:
            raise InvalidSourceError(f"No video with id {video_id}")
        source = self._source_path(video)
        streams = self.videos.streams(video.id)
        info = MediaInfo(video.format_name, video.duration_s, video.bit_rate, tuple(streams))
        ordered = [s for s in ALL_STAGES if s in set(stages)]
        report = PrepareReport(video.id)

        for position, stage in enumerate(ordered):
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Cancelled before " + stage.value)

            def stage_progress(fraction: float, message: str, _pos: int = position) -> None:
                if progress is not None:
                    progress((_pos + max(0.0, min(1.0, fraction))) / len(ordered), message)

            ctx = _Context(
                video=video,
                source=source,
                primary=info.primary_video,
                audio=info.audio_streams[0] if info.audio_streams else None,
                job_id=job_id,
                worker_id=worker_id,
                cancel=cancel,
                report=stage_progress,
            )
            report.outcomes.append(self._run_stage(stage, ctx, force=force))

        if report.failed:
            details = "; ".join(f"{o.stage.value}: {o.error}" for o in report.failed)
            raise ProcessingError(f"{len(report.failed)} stage(s) failed. {details}")
        return report

    def output_path(self, video: Video, stage: Stage, phash: str) -> Path:
        """Deterministic location of a stage's output."""
        names = {
            Stage.AUDIO: f"audio-16k-mono-{phash}.wav",
            Stage.PROXY: f"proxy-720p-{phash}.mp4",
            Stage.THUMBNAIL: f"thumbnail-{phash}.jpg",
        }
        return self.settings.derived_dir / video.sha256[:2] / video.sha256 / names[stage]

    def artifact_path(self, artifact: Artifact) -> Path:
        """Absolute path of a registered artifact, validated against the output roots."""
        return self._safe(self.settings.derived_dir / artifact.relpath)

    def artifact_is_valid(self, artifact: Artifact) -> bool:
        """True if the artifact file exists and matches its recorded size and SHA-256."""
        try:
            path = self.artifact_path(artifact)
        except OriginalProtectionError:
            return False
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size != artifact.size_bytes:
            return False
        return sha256_file(path) == artifact.sha256

    # ------------------------------------------------------------------ stages

    def _run_stage(self, stage: Stage, ctx: _Context, *, force: bool) -> StageOutcome:
        params = self._params(stage, ctx)
        phash = params_hash(params)

        not_applicable = self._not_applicable(stage, ctx)
        if not_applicable is not None:
            self.processing.record_skipped(
                ctx.video.id,
                stage,
                params_hash=phash,
                params=params,
                message=not_applicable,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            ctx.report(1.0, f"{stage.value}: skipped")
            return StageOutcome(stage, StageStatus.SKIPPED, not_applicable)

        kind = STAGE_ARTIFACT[stage]
        record = self.processing.start_stage(
            ctx.video.id,
            stage,
            params_hash=phash,
            params=params,
            job_id=ctx.job_id,
            worker_id=ctx.worker_id,
        )
        existing = self.processing.artifact(ctx.video.id, kind)
        if (
            not force
            and existing is not None
            and existing.params_hash == phash
            and self.artifact_is_valid(existing)
        ):
            message = "reused existing output"
            self.processing.finish_stage(
                record.id,
                StageStatus.SUCCEEDED,
                message=message,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            ctx.report(1.0, f"{stage.value}: reused")
            return StageOutcome(stage, StageStatus.SUCCEEDED, message, existing, reused=True)

        blocker = self._blocker(stage, ctx)
        if blocker is not None:
            self.processing.finish_stage(
                record.id,
                StageStatus.FAILED,
                error=blocker,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            return StageOutcome(stage, StageStatus.FAILED, "failed", error=blocker)

        final = self._safe(self.output_path(ctx.video, stage, phash))
        temp = self._safe(final.with_name(f".{final.name}.{uuid.uuid4().hex[:8]}{PARTIAL_SUFFIX}"))
        final.parent.mkdir(parents=True, exist_ok=True)
        last_write = [0.0]

        def on_fraction(fraction: float) -> None:
            ctx.report(fraction, f"{stage.value}: {int(fraction * 100)}%")
            now = time.monotonic()
            if now - last_write[0] >= _PROGRESS_WRITE_INTERVAL_S:
                last_write[0] = now
                self.processing.update_progress(record.id, fraction)

        ctx.report(0.0, f"{stage.value}: starting")
        try:
            metadata = self._produce(stage, ctx, temp, on_fraction)
            if ctx.cancel is not None and ctx.cancel.is_set():
                raise JobCancelledError(f"Cancelled during {stage.value}")
            size = temp.stat().st_size
            digest = sha256_file(temp)
            temp.replace(final)
            artifact, replaced = self.processing.register_artifact(
                video_id=ctx.video.id,
                stage_id=record.id,
                kind=kind,
                relpath=final.relative_to(self.settings.derived_dir.resolve()).as_posix(),
                size_bytes=size,
                sha256=digest,
                params_hash=phash,
                metadata=metadata,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            if replaced is not None:
                self._remove_replaced(replaced)
            message = _describe(stage, metadata)
            self.processing.finish_stage(
                record.id,
                StageStatus.SUCCEEDED,
                message=message,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            ctx.report(1.0, f"{stage.value}: done")
            return StageOutcome(stage, StageStatus.SUCCEEDED, message, artifact)
        except JobCancelledError:
            self.processing.finish_stage(
                record.id,
                StageStatus.CANCELLED,
                error="Cancelled by user",
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            raise
        except (ExternalToolError, MediaValidationError, ProbeError, OSError) as exc:
            error = str(exc) if isinstance(exc, IftyEditsError) else f"File error: {exc}"
            logger.warning("Stage %s failed for video %d: %s", stage, ctx.video.id, error)
            self.processing.finish_stage(
                record.id,
                StageStatus.FAILED,
                error=error,
                job_id=ctx.job_id,
                worker_id=ctx.worker_id,
            )
            return StageOutcome(stage, StageStatus.FAILED, "failed", error=error)
        finally:
            temp.unlink(missing_ok=True)

    def _produce(
        self, stage: Stage, ctx: _Context, temp: Path, on_fraction: Callable[[float], None]
    ) -> dict[str, Any]:
        if stage is Stage.AUDIO:
            return self._extract_audio(ctx, temp, on_fraction)
        if stage is Stage.PROXY:
            return self._make_proxy(ctx, temp, on_fraction)
        return self._make_thumbnail(ctx, temp, on_fraction)

    # ------------------------------------------------------------------ audio

    def _extract_audio(
        self, ctx: _Context, temp: Path, on_fraction: Callable[[float], None]
    ) -> dict[str, Any]:
        audio = ctx.audio
        if audio is None:  # guarded by _not_applicable
            raise MediaValidationError("No audio stream")
        args = [
            "-i",
            str(ctx.source),
            "-map",
            f"0:{audio.index}",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "-n",
            str(temp),
        ]
        self._transcode(args, ctx, temp, on_fraction)
        info = self._probe_output(temp)
        streams = info.audio_streams
        if len(streams) != 1 or info.video_streams:
            raise MediaValidationError("Audio output must contain exactly one audio stream")
        out = streams[0]
        if (out.codec_name, out.sample_rate, out.channels) != ("pcm_s16le", AUDIO_SAMPLE_RATE, 1):
            raise MediaValidationError(
                f"Audio output is {out.codec_name} {out.sample_rate} Hz {out.channels} ch, "
                "expected pcm_s16le 16000 Hz mono"
            )
        expected = audio.duration_s or ctx.video.duration_s
        if not durations_match(expected, info.duration_s):
            raise MediaValidationError(
                f"Audio duration {info.duration_s}s does not match source {expected}s"
            )
        return {
            "codec": "pcm_s16le",
            "sample_rate": AUDIO_SAMPLE_RATE,
            "channels": 1,
            "duration_s": info.duration_s,
            "source_stream_index": audio.index,
            "source_channels": audio.channels,
        }

    # ------------------------------------------------------------------ proxy

    def _make_proxy(
        self, ctx: _Context, temp: Path, on_fraction: Callable[[float], None]
    ) -> dict[str, Any]:
        primary = ctx.primary
        if primary is None:  # guarded by _blocker
            raise MediaValidationError("No video stream")
        encoder = self._h264()
        try:
            self._transcode(self._proxy_args(ctx, temp, encoder.name), ctx, temp, on_fraction)
            used = encoder
        except ExternalToolError as exc:
            if not encoder.hardware:
                raise
            logger.warning("VideoToolbox proxy encode failed, retrying with libx264: %s", exc)
            temp.unlink(missing_ok=True)
            used = EncoderInfo("libx264", False, f"VideoToolbox failed during encode: {exc}")
            self._transcode(self._proxy_args(ctx, temp, used.name), ctx, temp, on_fraction)

        info = self._probe_output(temp)
        out = info.primary_video
        if out is None or out.codec_name != "h264" or len(info.video_streams) != 1:
            raise MediaValidationError("Proxy must contain exactly one H.264 video stream")
        if out.width is None or out.height is None:
            raise MediaValidationError("Proxy frame size is unknown")
        if min(out.width, out.height) > PROXY_SHORT_SIDE or out.width % 2 or out.height % 2:
            raise MediaValidationError(f"Proxy size {out.width}x{out.height} is out of range")
        display = primary.display_size
        if display is not None and (out.width, out.height) != proxy_size(*display):
            raise MediaValidationError(
                f"Proxy size {out.width}x{out.height} does not match expected "
                f"{proxy_size(*display)} for a {display[0]}x{display[1]} display"
            )
        has_audio = ctx.audio is not None
        if bool(info.audio_streams) != has_audio:
            raise MediaValidationError("Proxy audio does not match the source")
        if not durations_match(ctx.video.duration_s, info.duration_s):
            raise MediaValidationError(
                f"Proxy duration {info.duration_s}s does not match source {ctx.video.duration_s}s"
            )
        return {
            "width": out.width,
            "height": out.height,
            "fps": proxy_fps(primary),
            "duration_s": info.duration_s,
            "has_audio": has_audio,
            "encoder": used.name,
            "hardware_encoded": used.hardware,
            "encoder_reason": used.reason,
            "source_rotation": primary.rotation,
            "source_vfr": primary.is_vfr,
            "source_hdr": primary.is_hdr,
        }

    def _proxy_args(self, ctx: _Context, temp: Path, encoder: str) -> list[str]:
        primary = ctx.primary
        if primary is None:
            raise MediaValidationError("No video stream")
        fps = proxy_fps(primary)
        gop = str(max(1, round(fps * 2)))
        args = ["-i", str(ctx.source), "-map", f"0:{primary.index}"]
        if ctx.audio is not None:
            args += ["-map", f"0:{ctx.audio.index}"]
        args += ["-vf", proxy_scale_filter(), "-fps_mode", "cfr", "-r", f"{fps:g}"]
        if encoder == "h264_videotoolbox":
            args += [
                "-c:v",
                "h264_videotoolbox",
                "-b:v",
                PROXY_VT_BITRATE,
                "-profile:v",
                "high",
                "-allow_sw",
                "0",
            ]
        else:
            args += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(PROXY_X264_CRF),
                "-profile:v",
                "high",
            ]
        args += ["-g", gop]
        if ctx.audio is not None:
            args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
        else:
            args += ["-an"]
        args += ["-sn", "-dn", "-map_metadata", "-1", "-movflags", "+faststart"]
        return [*args, "-f", "mp4", "-n", str(temp)]

    # ------------------------------------------------------------------ thumbnail

    def _thumbnail_input(self, ctx: _Context) -> tuple[Path, str, str, int]:
        """(path, label, content sha256, stream index) of the thumbnail source."""
        proxy = self.processing.artifact(ctx.video.id, STAGE_ARTIFACT[Stage.PROXY])
        if proxy is not None:
            path = self.artifact_path(proxy)
            if path.is_file() and path.stat().st_size == proxy.size_bytes:
                return path, "proxy", proxy.sha256, 0
        index = ctx.primary.index if ctx.primary is not None else 0
        return ctx.source, "original", ctx.video.sha256, index

    def _make_thumbnail(
        self, ctx: _Context, temp: Path, on_fraction: Callable[[float], None]
    ) -> dict[str, Any]:
        path, label, _, index = self._thumbnail_input(ctx)
        seek = thumbnail_seek(ctx.video.duration_s)
        try:
            self._transcode(self._thumbnail_args(path, index, seek, temp), ctx, temp, None)
        except ExternalToolError:
            if seek == 0:
                raise
            temp.unlink(missing_ok=True)
            seek = 0.0
            self._transcode(self._thumbnail_args(path, index, seek, temp), ctx, temp, None)
        if not temp.is_file() or temp.stat().st_size == 0:
            raise MediaValidationError("FFmpeg produced no thumbnail")
        on_fraction(1.0)
        info = self._probe_output(temp)
        out = info.primary_video
        if out is None or (out.width, out.height) != (THUMB_WIDTH, THUMB_HEIGHT):
            raise MediaValidationError("Thumbnail is not a 640x360 image")
        return {
            "width": THUMB_WIDTH,
            "height": THUMB_HEIGHT,
            "format": "jpeg",
            "input": label,
            "seek_s": seek,
        }

    @staticmethod
    def _thumbnail_args(path: Path, index: int, seek: float, temp: Path) -> list[str]:
        return [
            "-ss",
            f"{seek:.3f}",
            "-i",
            str(path),
            "-map",
            f"0:{index}",
            "-vf",
            thumbnail_filter(),
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-q:v",
            "3",
            "-f",
            "image2",
            "-update",
            "1",
            "-n",
            str(temp),
        ]

    # ------------------------------------------------------------------ helpers

    def _params(self, stage: Stage, ctx: _Context) -> dict[str, Any]:
        base: dict[str, Any] = {
            "stage": stage.value,
            "version": STAGE_VERSION[stage],
            "source_sha256": ctx.video.sha256,
        }
        if stage is Stage.AUDIO:
            stream = ctx.audio.index if ctx.audio else None
            return {
                **base,
                "sample_rate": AUDIO_SAMPLE_RATE,
                "channels": 1,
                "codec": "pcm_s16le",
                "stream": stream,
            }
        if stage is Stage.PROXY:
            fps = proxy_fps(ctx.primary) if ctx.primary else None
            return {
                **base,
                "short_side": PROXY_SHORT_SIDE,
                "fps": fps,
                "codec": "h264",
                "audio": "aac-128k-stereo" if ctx.audio else None,
            }
        _, label, input_sha, _ = self._thumbnail_input(ctx)
        return {
            **base,
            "width": THUMB_WIDTH,
            "height": THUMB_HEIGHT,
            "input": label,
            "input_sha256": input_sha,
            "sample_frames": THUMB_SAMPLE_FRAMES,
        }

    @staticmethod
    def _not_applicable(stage: Stage, ctx: _Context) -> str | None:
        if stage is Stage.AUDIO and ctx.video.status is VideoStatus.READY and ctx.audio is None:
            return "No audio stream in this video"
        return None

    @staticmethod
    def _blocker(stage: Stage, ctx: _Context) -> str | None:
        if ctx.video.status is VideoStatus.PROBE_FAILED:
            return "Video metadata is unavailable because probing failed at import"
        if stage in (Stage.PROXY, Stage.THUMBNAIL) and ctx.primary is None:
            return "No video stream recorded for this video"
        return None

    def _h264(self) -> EncoderInfo:
        caps = self.transcoder.capabilities()
        if caps.h264 is None:
            raise ExternalToolError(
                "No working H.264 encoder (h264_videotoolbox or libx264) in this FFmpeg"
            )
        return caps.h264

    def _transcode(
        self,
        args: list[str],
        ctx: _Context,
        temp: Path,
        on_fraction: Callable[[float], None] | None,
    ) -> None:
        duration = ctx.video.duration_s

        def on_seconds(seconds: float) -> None:
            if on_fraction is not None and duration:
                on_fraction(min(1.0, seconds / duration))

        self.transcoder.run(
            args,
            progress=on_seconds,
            cancel=ctx.cancel,
            timeout_s=self.settings.ffmpeg_timeout_s,
            redact=(ctx.source, temp, self.settings.data_dir),
        )

    def _probe_output(self, path: Path) -> MediaInfo:
        if not path.is_file() or path.stat().st_size == 0:
            raise MediaValidationError("FFmpeg produced no output")
        try:
            return self.prober.probe(path)
        except ProbeError as exc:
            raise MediaValidationError(f"Generated file cannot be read: {exc}") from exc

    def _source_path(self, video: Video) -> Path:
        source = self.settings.originals_dir / video.vault_relpath
        if not is_within(source, self.settings.originals_dir):
            raise OriginalProtectionError(f"Vault path escapes the vault: {video.vault_relpath}")
        if source.is_symlink() or not source.is_file():
            raise InvalidSourceError(
                f"Original for video {video.id} is missing from the vault: {video.vault_relpath}"
            )
        return source

    def _safe(self, path: Path) -> Path:
        return assert_safe_output_path(
            path,
            originals_root=self.settings.originals_dir,
            allowed_roots=(self.settings.derived_dir,),
        )

    def _remove_replaced(self, relpath: str) -> None:
        """Delete a superseded derived file that no artifact refers to any more."""
        if self.processing.relpath_in_use(relpath):
            return
        try:
            path = self._safe(self.settings.derived_dir / relpath)
        except OriginalProtectionError:
            logger.error("Not removing superseded artifact outside derived media: %s", relpath)
            return
        if path.is_file() and not path.is_symlink():
            path.unlink()


def _describe(stage: Stage, metadata: dict[str, Any]) -> str:
    if stage is Stage.AUDIO:
        return f"16 kHz mono WAV, {metadata.get('duration_s') or 0:.1f}s"
    if stage is Stage.PROXY:
        engine = "VideoToolbox" if metadata.get("hardware_encoded") else "libx264"
        return f"{metadata['width']}x{metadata['height']} H.264 ({engine})"
    return f"{metadata['width']}x{metadata['height']} JPEG from {metadata.get('input')}"
