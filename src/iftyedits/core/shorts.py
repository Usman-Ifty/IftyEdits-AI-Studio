"""Short-form edit settings, vertical crop geometry and render planning (Phase 3).

Everything here is pure: no I/O, no FFmpeg. ``ShortConfig`` is the complete, validated
description of one Short. It is stored as JSON on a project, hashed to detect duplicate
renders, and turned into FFmpeg arguments by ``adapters/short_render.py``. The preview
and the export use the same ``compute_crop`` result, so they frame the picture identically.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from typing import Any, Literal

from iftyedits.core.errors import ShortConfigError

TARGET_ASPECT = (9, 16)
MIN_CLIP_S = 3.0
MAX_CLIP_S = 180.0
FRAME_RATE = 30
CONFIG_VERSION = 1

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

SizeName = Literal["1080p", "720p", "540p"]
OUTPUT_SIZES: dict[str, tuple[int, int]] = {
    "1080p": (1080, 1920),
    "720p": (720, 1280),
    "540p": (540, 960),
}
SIZE_LABELS = {
    "1080p": "1080 x 1920 (full quality, default)",
    "720p": "720 x 1280 (smaller file)",
    "540p": "540 x 960 (draft)",
}
QUALITY_CRF = {"high": 18, "balanced": 21, "small": 25}
QUALITY_LABELS = {
    "high": "High (largest file)",
    "balanced": "Balanced",
    "small": "Small file",
}
# Platform UI covers the top and bottom of a Short. Captions stay between these fractions.
SAFE_TOP = 0.10
SAFE_BOTTOM = 0.80
SAFE_SIDE = 0.06

CAPTION_PRESETS: dict[str, dict[str, Any]] = {
    "classic": {
        "font_size": 64,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 6,
        "background": "none",
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": True,
    },
    "boxed": {
        "font_size": 58,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 0,
        "background": "box",
        "background_opacity": 0.72,
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": True,
    },
    "highlight": {
        "font_size": 70,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 8,
        "background": "none",
        "highlight": True,
        "highlight_color": "#FFD400",
        "bold": True,
    },
    "minimal": {
        "font_size": 52,
        "text_color": "#F2F2F2",
        "outline_color": "#202020",
        "outline_px": 3,
        "background": "none",
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": False,
    },
    "impact": {
        "font_size": 84,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 10,
        "background": "none",
        "highlight": True,
        "highlight_color": "#00E5FF",
        "bold": True,
        "max_lines": 2,
    },
    "studio": {
        "font_size": 56,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 0,
        "background": "box",
        "background_opacity": 0.55,
        "highlight": True,
        "highlight_color": "#8B7CFF",
        "bold": True,
    },
    "clean": {
        "font_size": 60,
        "text_color": "#FFFFFF",
        "outline_color": "#111111",
        "outline_px": 4,
        "background": "none",
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": True,
        "emphasis": True,
        "emphasis_color": "#FFD400",
        "position": 0.7,
    },
    "punchy": {
        "font_size": 88,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 11,
        "background": "none",
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": True,
        "emphasis": True,
        "emphasis_color": "#FF3D71",
        "max_lines": 2,
        "width_fraction": 0.8,
    },
    "cinematic": {
        "font_size": 48,
        "text_color": "#EDEDED",
        "outline_color": "#000000",
        "outline_px": 2,
        "background": "none",
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": False,
        "emphasis": False,
        "position": 0.76,
        "width_fraction": 0.74,
    },
    "mobile": {
        "font_size": 74,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_px": 0,
        "background": "box",
        "background_opacity": 0.85,
        "highlight": False,
        "highlight_color": "#FFD400",
        "bold": True,
        "emphasis": True,
        "emphasis_color": "#FFE600",
    },
}
PRESET_LABELS = {
    "classic": "Classic (white, black outline)",
    "boxed": "Boxed (white on dark box)",
    "highlight": "Karaoke (active word highlighted)",
    "minimal": "Minimal (smaller, thin outline)",
    "impact": "Impact (large, cyan active word)",
    "studio": "Studio (soft box, violet active word)",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _even_floor(value: float) -> int:
    return max(2, math.floor(value / 2.0) * 2)


def _even_round(value: float) -> int:
    return max(2, round(value / 2.0) * 2)


# ---------------------------------------------------------------------- crop geometry


@dataclass(frozen=True, slots=True)
class CropGeometry:
    """Where to cut a 9:16 picture out of the source frame, and how it is scaled."""

    source_w: int
    source_h: int
    x: int
    y: int
    width: int
    height: int
    out_w: int
    out_h: int

    @property
    def aspect_error(self) -> float:
        """Relative difference between the crop's aspect and exactly 9:16 (0 is exact)."""
        ideal = TARGET_ASPECT[0] / TARGET_ASPECT[1]
        return abs(self.width / self.height - ideal) / ideal

    @property
    def scale_factor(self) -> float:
        """Output pixels per source pixel (above 1 means the picture is enlarged)."""
        return self.out_h / self.height

    @property
    def is_full_frame(self) -> bool:
        """True when nothing is cut away (the source is already 9:16)."""
        return self.width == self.source_w and self.height == self.source_h

    @property
    def crop_filter(self) -> str:
        """The FFmpeg ``crop`` filter for this geometry."""
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def compute_crop(
    source_w: int,
    source_h: int,
    position: float = 0.5,
    *,
    out_size: str = "1080p",
) -> CropGeometry:
    """9:16 crop of a ``source_w`` x ``source_h`` frame.

    ``position`` is 0 (left edge) to 1 (right edge) and only matters when the source is
    wider than 9:16; values outside the range are clamped. Taller-than-9:16 sources are
    cropped top and bottom, centred. The crop is the largest window of the right shape:
    both sides are even (H.264 4:2:0 needs that), so a 1920x1080 frame crops to 606x1078
    (the ideal 607.5x1080 rounded down to an even width, with the height trimmed to match,
    leaving 0.04% aspect error instead of 0.25%). A source that is already 9:16 is not cut.
    The crop is scaled to ``out_size`` without stretching beyond that rounding.
    """
    if source_w < 2 or source_h < 2:
        raise ShortConfigError(f"The video frame is too small to reframe ({source_w}x{source_h})")
    if out_size not in OUTPUT_SIZES:
        raise ShortConfigError(f"Unknown output size {out_size!r}")
    out_w, out_h = OUTPUT_SIZES[out_size]
    pos = _clamp(position if math.isfinite(position) else 0.5, 0.0, 1.0)
    num, den = TARGET_ASPECT
    ideal = num / den
    src_w = source_w - source_w % 2
    src_h = source_h - source_h % 2
    if src_w * den == src_h * num:  # already 9:16: keep the whole frame
        return CropGeometry(source_w, source_h, 0, 0, src_w, src_h, out_w, out_h)
    if source_w / source_h > ideal:  # wider: keep the full height, slide a window sideways
        width = _even_floor(src_h * ideal)
        height = min(src_h, _even_round(width / ideal))
        x_range = src_w - width
        x = round(pos * x_range / 2.0) * 2
        x = min(x, x_range - x_range % 2)
        y = (src_h - height) // 2
        y -= y % 2
    else:  # taller (or narrower) than 9:16: keep the full width, trim top and bottom
        height = _even_floor(src_w / ideal)
        width = min(src_w, _even_round(height * ideal))
        height = min(height, src_h)
        x = (src_w - width) // 2
        x -= x % 2
        y = (src_h - height) // 2
        y -= y % 2
    return CropGeometry(source_w, source_h, x, y, width, height, out_w, out_h)


# ---------------------------------------------------------------------- settings


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    """How captions look and where they sit. Colours are ``#RRGGBB``."""

    enabled: bool = True
    preset: str = "classic"
    font_size: int = 64
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_px: int = 6
    background: Literal["none", "box"] = "none"
    background_opacity: float = 0.7
    highlight: bool = False
    highlight_color: str = "#FFD400"
    bold: bool = True
    emphasis: bool = False
    """Colour a few important words in each caption (chosen by a fixed rule, not at random)."""
    emphasis_color: str = "#FFD400"
    align: Literal["left", "center", "right"] = "center"
    position: float = 0.68
    """Vertical centre of the caption block as a fraction of the frame height."""
    width_fraction: float = 0.86
    """Widest a line may be, as a fraction of the frame width."""
    max_lines: int = 2

    def validate(self) -> CaptionStyle:
        """Check ranges and return the style with the vertical position clamped."""
        for name in ("text_color", "outline_color", "highlight_color", "emphasis_color"):
            if not HEX_COLOR.match(getattr(self, name)):
                raise ShortConfigError(f"Caption {name.replace('_', ' ')} must look like #FFFFFF")
        if not 28 <= self.font_size <= 140:
            raise ShortConfigError("Caption font size must be between 28 and 140")
        if not 0 <= self.outline_px <= 20:
            raise ShortConfigError("Caption outline must be between 0 and 20 pixels")
        if self.background not in ("none", "box"):
            raise ShortConfigError("Caption background must be 'none' or 'box'")
        if self.align not in ("left", "center", "right"):
            raise ShortConfigError("Caption alignment must be left, center or right")
        if not 0.3 <= self.width_fraction <= 1.0 - 2 * SAFE_SIDE + 1e-9:
            raise ShortConfigError("Caption width must be between 30% and 88% of the frame")
        if not 1 <= self.max_lines <= 4:
            raise ShortConfigError("Captions may use 1 to 4 lines")
        if not 0.0 <= self.background_opacity <= 1.0:
            raise ShortConfigError("Caption background opacity must be between 0 and 1")
        if not math.isfinite(self.position):
            raise ShortConfigError("Caption position must be a number")
        return replace(self, position=_clamp(self.position, SAFE_TOP, SAFE_BOTTOM))

    @classmethod
    def from_preset(cls, name: str, **overrides: Any) -> CaptionStyle:
        """A style from a named preset, with optional field overrides."""
        if name not in CAPTION_PRESETS:
            raise ShortConfigError(f"Unknown caption preset {name!r}")
        return cls(preset=name, **{**CAPTION_PRESETS[name], **overrides}).validate()


@dataclass(frozen=True, slots=True)
class AudioSettings:
    """Source-audio handling. Defaults leave the audio untouched."""

    keep: bool = True
    volume_percent: int = 100
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    normalize: bool = False
    """Single-pass EBU R128 loudness normalisation to about -16 LUFS (speech friendly)."""

    def validate(self) -> AudioSettings:
        """Check ranges."""
        if not 0 <= self.volume_percent <= 200:
            raise ShortConfigError("Volume must be between 0% and 200%")
        for name in ("fade_in_s", "fade_out_s"):
            value = getattr(self, name)
            if not (math.isfinite(value) and 0.0 <= value <= 5.0):
                raise ShortConfigError("Audio fades must be between 0 and 5 seconds")
        return self


@dataclass(frozen=True, slots=True)
class PolishSettings:
    """Light picture adjustments. Defaults change nothing."""

    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0

    def validate(self) -> PolishSettings:
        """Check ranges."""
        for name in ("fade_in_s", "fade_out_s"):
            value = getattr(self, name)
            if not (math.isfinite(value) and 0.0 <= value <= 3.0):
                raise ShortConfigError("Picture fades must be between 0 and 3 seconds")
        if not -0.3 <= self.brightness <= 0.3:
            raise ShortConfigError("Brightness must be between -0.3 and 0.3")
        if not 0.7 <= self.contrast <= 1.4:
            raise ShortConfigError("Contrast must be between 0.7 and 1.4")
        if not 0.0 <= self.saturation <= 2.0:
            raise ShortConfigError("Saturation must be between 0 and 2")
        return self

    @property
    def has_color_change(self) -> bool:
        """True when brightness, contrast or saturation differ from neutral."""
        return (self.brightness, self.contrast, self.saturation) != (0.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class OutputSettings:
    """Size and quality of the finished file."""

    size: str = "1080p"
    quality: str = "high"

    def validate(self) -> OutputSettings:
        """Check the names."""
        if self.size not in OUTPUT_SIZES:
            raise ShortConfigError(f"Unknown output size {self.size!r}")
        if self.quality not in QUALITY_CRF:
            raise ShortConfigError(f"Unknown quality {self.quality!r}")
        return self


@dataclass(frozen=True, slots=True)
class ShortConfig:
    """Everything that decides how one Short looks. Times are source-video seconds."""

    start_s: float
    end_s: float
    crop_position: float = 0.5
    crop_path: tuple[tuple[float, float], ...] = ()
    """Optional moving window as ``(seconds from clip start, position)`` keyframes. Empty means
    the steady ``crop_position``. Set by automatic framing; changing the crop by hand clears it."""
    captions: CaptionStyle = field(default_factory=CaptionStyle)
    text_overrides: dict[int, str] = field(default_factory=dict)
    """Corrected caption text by transcript segment position. The transcript is not changed."""
    audio: AudioSettings = field(default_factory=AudioSettings)
    polish: PolishSettings = field(default_factory=PolishSettings)
    output: OutputSettings = field(default_factory=OutputSettings)

    @property
    def duration_s(self) -> float:
        """Length of the clip in seconds."""
        return self.end_s - self.start_s

    def validate(self, source_duration_s: float | None = None) -> ShortConfig:
        """Return a validated copy; raise :class:`ShortConfigError` with a clear reason."""
        start, end = validate_range(self.start_s, self.end_s, source_duration_s)
        if not math.isfinite(self.crop_position):
            raise ShortConfigError("Crop position must be a number")
        path = _clean_path(self.crop_path, end - start)
        for key, text in self.text_overrides.items():
            if key < 0:
                raise ShortConfigError("Caption corrections refer to a transcript segment")
            if len(text) > 500 or "\x00" in text:
                raise ShortConfigError("A corrected caption is too long or contains bad characters")
        audio = self.audio.validate()
        if audio.fade_in_s + audio.fade_out_s > end - start:
            raise ShortConfigError("Audio fades are longer than the clip")
        polish = self.polish.validate()
        if polish.fade_in_s + polish.fade_out_s > end - start:
            raise ShortConfigError("Picture fades are longer than the clip")
        return replace(
            self,
            start_s=start,
            end_s=end,
            crop_position=_clamp(self.crop_position, 0.0, 1.0),
            crop_path=path,
            captions=self.captions.validate(),
            audio=audio,
            polish=polish,
            output=self.output.validate(),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (text-override keys become strings)."""
        data = asdict(self)
        data["text_overrides"] = {str(k): v for k, v in sorted(self.text_overrides.items())}
        data["version"] = CONFIG_VERSION
        captions = data["captions"]
        if not captions.get("emphasis"):
            captions.pop("emphasis", None)  # keeps the hash of settings made before emphasis
            captions.pop("emphasis_color", None)
        if self.crop_path:
            data["crop_path"] = [[t, p] for t, p in self.crop_path]
        else:
            data.pop("crop_path", None)  # keeps the hash of every pre-existing setting
        return data

    def to_json(self) -> str:
        """Canonical JSON (stable key order)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShortConfig:
        """Rebuild from :meth:`to_dict` output; unknown keys are ignored."""
        try:
            return cls(
                start_s=float(data["start_s"]),
                end_s=float(data["end_s"]),
                crop_position=float(data.get("crop_position", 0.5)),
                crop_path=tuple((float(t), float(p)) for t, p in data.get("crop_path", ())),
                captions=_build(CaptionStyle, data.get("captions")),
                text_overrides={int(k): str(v) for k, v in data.get("text_overrides", {}).items()},
                audio=_build(AudioSettings, data.get("audio")),
                polish=_build(PolishSettings, data.get("polish")),
                output=_build(OutputSettings, data.get("output")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ShortConfigError(f"Stored Short settings are unreadable: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> ShortConfig:
        """Rebuild from :meth:`to_json` output."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ShortConfigError(f"Stored Short settings are not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ShortConfigError("Stored Short settings are not an object")
        return cls.from_dict(data)

    def config_hash(self, *, video_sha256: str, kind: str, has_audio: bool) -> str:
        """Stable id of what a render would produce (source, settings, preview or export)."""
        text = f"{CONFIG_VERSION}|{kind}|{video_sha256}|{int(has_audio)}|{self.to_json()}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


MAX_PATH_KEYFRAMES = 32


def _clean_path(
    path: tuple[tuple[float, float], ...], duration_s: float
) -> tuple[tuple[float, float], ...]:
    """Validated moving-crop keyframes: finite, in range, strictly increasing in time."""
    if len(path) > MAX_PATH_KEYFRAMES:
        raise ShortConfigError("The moving crop has too many keyframes")
    clean: list[tuple[float, float]] = []
    for t, p in path:
        if not (math.isfinite(t) and math.isfinite(p)):
            raise ShortConfigError("Moving crop keyframes must be numbers")
        t = _clamp(t, 0.0, max(duration_s, 0.0))
        if clean and t <= clean[-1][0]:
            raise ShortConfigError("Moving crop keyframes must go forward in time")
        clean.append((round(t, 3), round(_clamp(p, 0.0, 1.0), 4)))
    return tuple(clean)


def crop_x_expression(crop: CropGeometry, path: tuple[tuple[float, float], ...]) -> str:
    """FFmpeg ``x`` expression sliding the window along ``path`` (a number if it is steady)."""
    span = (crop.source_w - crop.source_w % 2) - crop.width
    if span <= 0 or not path:
        return str(crop.x)
    pts = [(t, round(p * span, 2)) for t, p in path]
    expr = f"{pts[-1][1]:g}"
    for (t0, x0), (t1, x1) in reversed(list(pairwise(pts))):
        seg = f"{x0:g}" if t1 <= t0 else f"{x0:g}+({x1 - x0:g})*(t-{t0:g})/{t1 - t0:g}"
        expr = f"if(lt(t,{t1:g}),{seg},{expr})"
    if len(pts) > 1:
        expr = f"if(lt(t,{pts[0][0]:g}),{pts[0][1]:g},{expr})"
    return expr


def _build(cls: Any, data: Any) -> Any:
    """Construct a settings dataclass from a dict, ignoring unknown keys."""
    if not isinstance(data, dict):
        return cls()
    names = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in names})


def validate_range(
    start_s: float, end_s: float, source_duration_s: float | None = None
) -> tuple[float, float]:
    """Check a clip range; returns it rounded to milliseconds.

    Rejects non-numbers, negative or reversed ranges, clips shorter than ``MIN_CLIP_S``
    or longer than ``MAX_CLIP_S`` and clips that run past the end of the source.
    """
    if not (math.isfinite(start_s) and math.isfinite(end_s)):
        raise ShortConfigError("Start and end must be numbers")
    start, end = round(start_s, 3), round(end_s, 3)
    if start < 0:
        raise ShortConfigError("The clip cannot start before the beginning of the video")
    if end <= start:
        raise ShortConfigError("The clip must end after it starts")
    if end - start < MIN_CLIP_S:
        raise ShortConfigError(f"A Short must be at least {MIN_CLIP_S:g} seconds long")
    if end - start > MAX_CLIP_S:
        raise ShortConfigError(f"A Short can be at most {MAX_CLIP_S:g} seconds long")
    if source_duration_s is not None and end > source_duration_s + 0.001:
        raise ShortConfigError(
            f"The clip ends at {end:.1f}s but the video is only {source_duration_s:.1f}s long"
        )
    return start, end
