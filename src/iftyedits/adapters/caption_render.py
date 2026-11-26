"""Draw captions with Pillow as transparent PNGs (the installed FFmpeg has no text filters).

The FFmpeg in this project lacks ``subtitles``, ``ass`` and ``drawtext`` (no libass or
freetype), so captions are drawn here and laid over the video with the ``overlay`` filter.
The same drawing code produces the picture in the editor preview and the frames used by the
export, so what you see is what is rendered.

Fonts are looked up in a short list of system locations. The first one that exists and
covers the text's script is used; if none is found Pillow's built-in font is used (Latin
only, and the editor says so). No font file is bundled.
"""

from __future__ import annotations

import functools
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from iftyedits.core.captions import CaptionCue, emphasis_indexes
from iftyedits.core.shorts import SAFE_BOTTOM, SAFE_SIDE, SAFE_TOP, CaptionStyle

_BOLD_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)
_REGULAR_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_WIDE_SCRIPT_FONTS = (
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", 0),
)
_ARABIC_HEBREW = (
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/System/Library/Fonts/GeezaPro.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
)
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
Color = tuple[int, int, int, int]


def _rgba(hex_color: str, alpha: int = 255) -> Color:
    value = hex_color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def _needs_wide_script(text: str) -> str:
    """'cjk', 'rtl' or '' depending on the scripts present."""
    kind = ""
    for char in text:
        code = ord(char)
        if code < 0x250:
            continue
        name = unicodedata.name(char, "")
        if any(
            tag in name
            for tag in ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "BOPOMOFO", "IDEOGRAPH")
        ):
            return "cjk"
        if "ARABIC" in name or "HEBREW" in name:
            kind = "rtl"
    return kind


@functools.lru_cache(maxsize=64)
def _load(path: str, index: int, size: int) -> Font:
    try:
        return ImageFont.truetype(path, size=size, index=index)
    except OSError:
        return ImageFont.load_default(size)


def find_font(size: int, *, bold: bool, text: str = "") -> tuple[Font, str]:
    """A font for ``text`` and a note on what was used (``''`` for the normal case)."""
    script = _needs_wide_script(text)
    if script:
        choices = _WIDE_SCRIPT_FONTS if script == "cjk" else _ARABIC_HEBREW
        for candidate, index in choices:
            if Path(candidate).is_file():
                return _load(candidate, index, size), ""
        return _load("", 0, size), "no installed font covers this script; shown with a basic font"
    for candidate in _BOLD_FONTS if bold else _REGULAR_FONTS:
        if Path(candidate).is_file():
            return _load(candidate, 0, size), ""
    return _load("", 0, size), "no system font found; using Pillow's basic font"


def fonts_available() -> bool:
    """True when a real TrueType font exists for Latin text."""
    return any(Path(p).is_file() for p in _BOLD_FONTS + _REGULAR_FONTS)


class _Metrics:
    """Measures words, choosing a font per word so mixed-script lines render fully."""

    def __init__(self, size: int, bold: bool) -> None:
        self.size = size
        self.bold = bold
        self.note = ""
        self.base, _ = find_font(size, bold=bold)
        self.space = float(self.base.getlength(" "))

    def font(self, word: str) -> Font:
        """Font for one word."""
        font, note = find_font(self.size, bold=self.bold, text=word)
        self.note = self.note or note
        return font

    def width(self, word: str) -> float:
        """Pixel width of one word."""
        return float(self.font(word).getlength(word))

    def line_width(self, words: Sequence[str]) -> float:
        """Pixel width of words joined by spaces."""
        if not words:
            return 0.0
        return sum(self.width(w) for w in words) + self.space * (len(words) - 1)


def wrap_words(words: Sequence[str], metrics: _Metrics, max_width: float) -> list[list[str]]:
    """Greedy line wrap. A word wider than a line gets a line of its own."""
    lines: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and metrics.line_width([*current, word]) > max_width:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines


@dataclass(frozen=True, slots=True)
class CaptionLayout:
    """Where a rendered caption block sits (pixels) and how it was fitted."""

    left: int
    top: int
    right: int
    bottom: int
    font_size: int
    lines: int
    note: str = ""

    @property
    def inside_safe_area(self) -> bool:
        """True if the block lies inside the frame it was laid out for."""
        return self.left >= 0 and self.top >= 0


def render_cue(
    cue: CaptionCue,
    style: CaptionStyle,
    frame: tuple[int, int],
    *,
    active_word: int | None = None,
) -> tuple[Image.Image, CaptionLayout]:
    """Draw one caption on a transparent RGBA frame of size ``frame``.

    ``active_word`` is the index of the word to colour with the highlight colour (only used
    when the style asks for highlighting and the cue has real word timing).
    """
    width, height = frame
    scale = height / 1920.0
    size = max(14, round(style.font_size * scale))
    outline = round(style.outline_px * scale)
    max_text_w = width * style.width_fraction - (2 * 24 * scale if style.background == "box" else 0)
    max_text_w = min(max_text_w, width * (1 - 2 * SAFE_SIDE))
    words = [w.text for w in cue.words] if cue.words else cue.text.split()
    while True:
        metrics = _Metrics(size, style.bold)
        lines = wrap_words(words, metrics, max_text_w)
        widest = max((metrics.line_width(line) for line in lines), default=0.0)
        if (len(lines) <= style.max_lines and widest <= max_text_w) or size <= 14:
            break
        size = max(14, int(size * 0.92))
    image = Image.new("RGBA", frame, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ascent, descent = (size, size // 4)
    if isinstance(metrics.base, ImageFont.FreeTypeFont):
        ascent, descent = metrics.base.getmetrics()
    line_h = int((ascent + descent) * 1.12)
    pad = round(22 * scale) if style.background == "box" else 0
    block_w = int(widest) + 2 * pad + 2 * outline
    block_h = line_h * len(lines) + 2 * pad + 2 * outline
    left = round((width - block_w) / 2)
    center_y = style.position * height
    top = round(center_y - block_h / 2)
    top = max(int(SAFE_TOP * height), min(top, int(SAFE_BOTTOM * height) - block_h))
    top = max(0, top)
    left = max(0, min(left, width - block_w))
    if style.background == "box":
        alpha = round(255 * style.background_opacity)
        draw.rounded_rectangle(
            (left, top, left + block_w, top + block_h),
            radius=round(18 * scale),
            fill=(0, 0, 0, alpha),
        )
    fill = _rgba(style.text_color)
    stroke = _rgba(style.outline_color)
    index = 0
    stressed = emphasis_indexes(words) if style.emphasis else frozenset[int]()
    for line_no, line in enumerate(lines):
        y = top + pad + outline + line_no * line_h
        line_w = metrics.line_width(line)
        x: float
        inner_left = left + pad + outline
        inner_w = block_w - 2 * pad - 2 * outline
        if style.align == "left":
            x = inner_left
        elif style.align == "right":
            x = inner_left + inner_w - line_w
        else:
            x = inner_left + (inner_w - line_w) / 2
        for word in line:
            colour = _rgba(style.emphasis_color) if index in stressed else fill
            if style.highlight and active_word is not None and index == active_word:
                colour = _rgba(style.highlight_color)
            draw.text(
                (x, y),
                word,
                font=metrics.font(word),
                fill=colour,
                stroke_width=outline,
                stroke_fill=stroke,
            )
            x += metrics.width(word) + metrics.space
            index += 1
    layout = CaptionLayout(left, top, left + block_w, top + block_h, size, len(lines), metrics.note)
    return image, layout


def cue_states(cue: CaptionCue, style: CaptionStyle) -> list[tuple[float, float, int | None]]:
    """``(start, end, active_word)`` pieces of a cue (one piece unless highlighting words)."""
    if not (style.highlight and cue.can_highlight):
        return [(cue.start_s, cue.end_s, None)]
    states: list[tuple[float, float, int | None]] = []
    for index, word in enumerate(cue.words):
        start = cue.start_s if index == 0 else word.start_s
        end = cue.end_s if index == len(cue.words) - 1 else cue.words[index + 1].start_s
        start, end = max(start, cue.start_s), min(max(end, start + 0.04), cue.end_s)
        if end > start:
            states.append((start, end, index))
    return states or [(cue.start_s, cue.end_s, None)]
