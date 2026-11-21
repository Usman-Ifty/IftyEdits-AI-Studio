"""Build and verify the FFmpeg render of a Short.

One filter graph serves preview and export, so the crop, captions and polish match; only
the output size, quality and encoder settings differ. Captions are Pillow-drawn PNG frames
(see :mod:`iftyedits.adapters.caption_render`) read through the concat demuxer and laid
over the picture with ``overlay``, because the installed FFmpeg has no text filters.

Every value that reaches an FFmpeg argument is a number we computed or a path we built; no
user text is placed in a filter string. Caption text only ever goes into PNG pixels.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from iftyedits.adapters.caption_render import cue_states as _cue_states
from iftyedits.adapters.caption_render import render_cue
from iftyedits.adapters.ffmpeg import HARDWARE_H264, SOFTWARE_H264
from iftyedits.core.captions import CaptionCue
from iftyedits.core.errors import ExternalToolError, ShortRenderError
from iftyedits.core.protocols import MediaProber, MediaTranscoder
from iftyedits.core.shorts import (
    OUTPUT_SIZES,
    QUALITY_CRF,
    CaptionStyle,
    CropGeometry,
    ShortConfig,
)

DURATION_TOLERANCE_S = 0.5
AV_DRIFT_TOLERANCE_S = 0.5
PREVIEW_CRF = 28
_BASE_BITRATE_1080 = {"high": 14_000_000, "balanced": 9_000_000, "small": 6_000_000}


def target_fps(source_fps: float | None) -> int:
    """Constant output frame rate: 60 for high-rate sources, otherwise 30."""
    return 60 if source_fps is not None and source_fps >= 50 else 30


# ---------------------------------------------------------------------- captions


@dataclass(frozen=True, slots=True)
class CaptionAssets:
    """The overlay image sequence for a render."""

    list_path: Path
    frames: int


def _concat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def write_caption_assets(
    cues: Sequence[CaptionCue],
    style: CaptionStyle,
    frame: tuple[int, int],
    duration_s: float,
    work_dir: Path,
) -> CaptionAssets | None:
    """Draw every caption state and write the concat list covering the whole clip.

    Returns ``None`` when there is nothing to show. Gaps use a fully transparent frame.
    """
    if not style.enabled or not cues:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    blank = work_dir / "blank.png"
    Image.new("RGBA", frame, (0, 0, 0, 0)).save(blank)
    timeline: list[tuple[float, Path]] = []  # (duration, file)
    cursor = 0.0
    count = 0
    for cue in cues:
        for start, end, active in _cue_states(cue, style):
            start = max(start, cursor)
            end = min(end, duration_s)
            if end - start < 0.02:
                continue
            if start - cursor > 0.001:
                timeline.append((start - cursor, blank))
            image, _ = render_cue(cue, style, frame, active_word=active)
            count += 1
            target = work_dir / f"cap_{count:04d}.png"
            image.save(target)
            timeline.append((end - start, target))
            cursor = end
    if not timeline:
        return None
    if duration_s - cursor > 0.001:
        timeline.append((duration_s - cursor, blank))
    lines = ["ffconcat version 1.0"]
    for length, path in timeline:
        lines.append(f"file {_concat_quote(path)}")
        lines.append(f"duration {length:.3f}")
    lines.append(f"file {_concat_quote(timeline[-1][1])}")  # the demuxer needs a final entry
    list_path = work_dir / "captions.ffconcat"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CaptionAssets(list_path, count)


# ---------------------------------------------------------------------- command


@dataclass(frozen=True, slots=True)
class EncodeChoice:
    """Encoder used for one render."""

    name: str
    hardware: bool


def pick_encoder(preference: str, hardware_verified: bool) -> EncodeChoice:
    """VideoToolbox only when verified by a test encode and not turned off; else libx264."""
    if preference != SOFTWARE_H264 and hardware_verified:
        return EncodeChoice(HARDWARE_H264, True)
    return EncodeChoice(SOFTWARE_H264, False)


def _video_chain(
    config: ShortConfig, crop: CropGeometry, fps: int, has_captions: bool, duration_s: float
) -> str:
    steps = [
        f"fps={fps}",
        f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y}",
        f"scale={crop.out_w}:{crop.out_h}:flags=lanczos",
        "setsar=1",
    ]
    polish = config.polish
    if polish.has_color_change:
        steps.append(
            f"eq=brightness={polish.brightness:.3f}:contrast={polish.contrast:.3f}"
            f":saturation={polish.saturation:.3f}"
        )
    graph = "[0:v:0]" + ",".join(steps) + "[base]"
    last = "base"
    if has_captions:
        graph += ";[base][1:v]overlay=0:0:eof_action=pass:format=auto[lay]"
        last = "lay"
    tail: list[str] = []
    if polish.fade_in_s > 0:
        tail.append(f"fade=t=in:st=0:d={polish.fade_in_s:.3f}")
    if polish.fade_out_s > 0:
        start = max(0.0, duration_s - polish.fade_out_s)
        tail.append(f"fade=t=out:st={start:.3f}:d={polish.fade_out_s:.3f}")
    tail.append("format=yuv420p")
    return f"{graph};[{last}]{','.join(tail)}[v]"


def _audio_chain(config: ShortConfig, duration_s: float, limiter: bool) -> str:
    audio = config.audio
    steps: list[str] = []
    if audio.normalize:
        steps += ["loudnorm=I=-16:TP=-1.5:LRA=11", "aresample=48000"]
    if audio.volume_percent != 100:
        steps.append(f"volume={audio.volume_percent / 100:.3f}")
        if audio.volume_percent > 100 and limiter:
            steps.append("alimiter=limit=0.95")
    if audio.fade_in_s > 0:
        steps.append(f"afade=t=in:st=0:d={audio.fade_in_s:.3f}")
    if audio.fade_out_s > 0:
        start = max(0.0, duration_s - audio.fade_out_s)
        steps.append(f"afade=t=out:st={start:.3f}:d={audio.fade_out_s:.3f}")
    steps.append("aresample=48000")
    return "[0:a:0]" + ",".join(steps) + "[a]"


def build_args(
    config: ShortConfig,
    *,
    source: Path,
    output: Path,
    crop: CropGeometry,
    source_fps: float | None,
    has_audio: bool,
    captions: CaptionAssets | None,
    encoder: EncodeChoice,
    preview: bool,
    limiter: bool = True,
) -> list[str]:
    """FFmpeg arguments (without the binary) that render ``config`` to ``output``."""
    duration = config.duration_s
    fps = target_fps(source_fps)
    graph = _video_chain(config, crop, fps, captions is not None, duration)
    keep_audio = has_audio and config.audio.keep
    if keep_audio:
        graph += ";" + _audio_chain(config, duration, limiter)
    args = ["-y", "-ss", f"{config.start_s:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    if captions is not None:
        args += ["-f", "concat", "-safe", "0", "-i", str(captions.list_path)]
    args += ["-filter_complex", graph, "-map", "[v]"]
    if keep_audio:
        args += ["-map", "[a]"]
    else:
        args += ["-an"]
    quality = config.output.quality
    pixels = crop.out_w * crop.out_h / float(OUTPUT_SIZES["1080p"][0] * OUTPUT_SIZES["1080p"][1])
    if encoder.hardware:
        rate = _BASE_BITRATE_1080[quality] * pixels * (0.35 if preview else 1.0)
        args += ["-c:v", encoder.name, "-b:v", str(int(rate)), "-profile:v", "high"]
    else:
        crf = PREVIEW_CRF if preview else QUALITY_CRF[quality]
        preset = "veryfast" if preview else "medium"
        args += ["-c:v", encoder.name, "-preset", preset, "-crf", str(crf), "-profile:v", "high"]
    args += ["-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2)]
    if keep_audio:
        args += ["-c:a", "aac", "-b:a", "128k" if preview else "192k", "-ar", "48000", "-ac", "2"]
    args += ["-movflags", "+faststart", "-map_metadata", "-1", "-f", "mp4", str(output)]
    return args


# ---------------------------------------------------------------------- verification


@dataclass(frozen=True, slots=True)
class VerifiedOutput:
    """Facts read back from a finished file."""

    width: int
    height: int
    duration_s: float
    size_bytes: int
    has_audio: bool
    video_codec: str


def verify_output(
    path: Path,
    *,
    expect_size: tuple[int, int],
    expect_duration_s: float,
    expect_audio: bool,
    prober: MediaProber,
    ffmpeg: MediaTranscoder,
) -> VerifiedOutput:
    """Probe and fully decode ``path``; raise :class:`ShortRenderError` on any mismatch."""
    if not path.is_file() or path.stat().st_size <= 0:
        raise ShortRenderError("The export is missing or empty")
    try:
        info = prober.probe(path)
    except ExternalToolError as exc:
        raise ShortRenderError(f"The export could not be read back: {exc}") from exc
    video = info.primary_video
    if video is None:
        raise ShortRenderError("The export has no video stream")
    if video.codec_name != "h264":
        raise ShortRenderError(f"The export is {video.codec_name}, expected h264")
    if video.pix_fmt != "yuv420p":
        raise ShortRenderError(f"The export pixel format is {video.pix_fmt}, expected yuv420p")
    size = video.display_size
    if size != expect_size:
        raise ShortRenderError(f"The export is {size}, expected {expect_size}")
    duration = info.duration_s or video.duration_s
    if duration is None or abs(duration - expect_duration_s) > DURATION_TOLERANCE_S:
        raise ShortRenderError(
            f"The export lasts {duration}s, expected about {expect_duration_s:.2f}s"
        )
    audio = info.audio_streams
    if expect_audio and not audio:
        raise ShortRenderError("The export lost its audio track")
    if not expect_audio and audio:
        raise ShortRenderError("The export has audio but none was expected")
    if audio and audio[0].codec_name != "aac":
        raise ShortRenderError(f"The export audio is {audio[0].codec_name}, expected aac")
    a_len = audio[0].duration_s if audio else None
    v_len = video.duration_s
    if a_len is not None and v_len is not None and abs(a_len - v_len) > AV_DRIFT_TOLERANCE_S:
        raise ShortRenderError("Audio and video lengths differ; they may be out of sync")
    try:
        ffmpeg.run(["-xerror", "-i", str(path), "-f", "null", "-"], timeout_s=600)
    except ExternalToolError as exc:
        raise ShortRenderError(f"The export failed a full decode: {exc}") from exc
    return VerifiedOutput(
        width=size[0],
        height=size[1],
        duration_s=float(duration),
        size_bytes=path.stat().st_size,
        has_audio=bool(audio),
        video_codec="h264",
    )


# ---------------------------------------------------------------------- stills


def extract_frame(ffmpeg: MediaTranscoder, source: Path, at_s: float, output: Path) -> Path:
    """Save one source frame (rotation applied) as PNG."""
    ffmpeg.run(
        ["-y", "-ss", f"{max(0.0, at_s):.3f}", "-i", str(source), "-frames:v", "1", str(output)],
        timeout_s=120,
    )
    if not output.is_file():
        raise ShortRenderError("Could not read a picture from the video at that time")
    return output


def crop_guide(frame: Image.Image, crop: CropGeometry) -> Image.Image:
    """The source frame with the vertical window outlined and the outside dimmed."""
    base = frame.convert("RGBA")
    sx = base.width / crop.source_w
    sy = base.height / crop.source_h
    box = (
        round(crop.x * sx),
        round(crop.y * sy),
        round((crop.x + crop.width) * sx),
        round((crop.y + crop.height) * sy),
    )
    shade = Image.new("RGBA", base.size, (0, 0, 0, 150))
    ImageDraw.Draw(shade).rectangle(box, fill=(0, 0, 0, 0))
    out = Image.alpha_composite(base, shade)
    ImageDraw.Draw(out).rectangle(box, outline=(255, 212, 0, 255), width=max(2, base.width // 240))
    return out.convert("RGB")


def framed_still(
    frame: Image.Image,
    crop: CropGeometry,
    cue: CaptionCue | None,
    style: CaptionStyle,
    *,
    out_height: int = 960,
) -> Image.Image:
    """What the Short looks like at this frame: cropped, scaled, with the caption drawn on."""
    sx = frame.width / crop.source_w
    sy = frame.height / crop.source_h
    box = (
        round(crop.x * sx),
        round(crop.y * sy),
        round((crop.x + crop.width) * sx),
        round((crop.y + crop.height) * sy),
    )
    out_w = round(out_height * 9 / 16)
    out_w -= out_w % 2
    picture = frame.convert("RGB").crop(box).resize((out_w, out_height), Image.Resampling.LANCZOS)
    if cue is not None and style.enabled:
        overlay, _ = render_cue(cue, style, (out_w, out_height))
        picture = Image.alpha_composite(picture.convert("RGBA"), overlay).convert("RGB")
    return picture


def expected_size(crop: CropGeometry) -> tuple[int, int]:
    """Output size the geometry produces."""
    return crop.out_w, crop.out_h


def is_close(a: float, b: float, tol: float) -> bool:
    """Absolute-difference comparison that treats NaN as not close."""
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol
