"""Draw captions with Pillow as transparent PNGs (the installed FFmpeg has no text filters).

The FFmpeg in this project lacks ``subtitles``, ``ass`` and ``drawtext`` (no libass or
freetype), so captions are drawn here and laid over the video with the ``overlay`` filter.
The same drawing code produces the picture in the editor preview and the frames used by the
export, so what you see is what is rendered.

Fonts are looked up in a short list of system locations. The first one that exists and
covers the text's script is used; if none is found Pillow's built-in font is used (Latin
only, and the editor says so). No font file is bundled.

Devanagari (Hindi, Marathi, Nepali) also needs complex text shaping, which Pillow does
through Raqm and the FriBiDi library. Without it the letters come out in the wrong order, so
captions are refused with a message instead. On macOS, Python does not search Homebrew's
library folder by itself; see :func:`shaping_environment`.
"""

from __future__ import annotations

import functools
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from iftyedits.core.captions import CaptionCue, emphasis_indexes
from iftyedits.core.errors import ShortRenderError
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
_DEVANAGARI_FONTS = (  # (file, regular face, bold face)
    ("/System/Library/Fonts/Kohinoor.ttc", 0, 3),
    ("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc", 0, 1),
    ("/System/Library/Fonts/Supplemental/DevanagariMT.ttc", 0, 1),
)
_DEVANAGARI_FILES = (  # (regular file, bold file)
    (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    ),
)
_DEVANAGARI_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))
_HOMEBREW_LIB = Path("/opt/homebrew/lib")
_DYLD_VARIABLE = "DYLD_FALLBACK_LIBRARY_PATH"
SHAPING_HELP = (
    "Hindi captions need the FriBiDi library to draw letters correctly. Run "
    "`brew install fribidi`, restart the studio with `uv run iftyedits ui`, then export "
    "again. You can also turn captions off for this Short."
)
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
Color = tuple[int, int, int, int]


def _rgba(hex_color: str, alpha: int = 255) -> Color:
    value = hex_color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def shaping_environment(
    env: Mapping[str, str], *, platform: str = sys.platform, library_dir: Path = _HOMEBREW_LIB
) -> dict[str, str] | None:
    """``env`` extended so a new process can load Homebrew's FriBiDi, or ``None``.

    macOS reads the variable only when a process starts, so the caller must start (or
    restart) a process with the result. ``None`` means there is nothing to change.
    """
    if platform != "darwin" or not (library_dir / "libfribidi.dylib").is_file():
        return None
    current = env.get(_DYLD_VARIABLE) or "/usr/local/lib:/usr/lib"
    folders = current.split(":")
    if str(library_dir) in folders:
        return None
    return {**env, _DYLD_VARIABLE: ":".join([*folders, str(library_dir)])}


def shaping_available() -> bool:
    """True when Pillow can shape complex scripts (Raqm with FriBiDi loaded)."""
    return bool(features.check("raqm"))


def _is_devanagari(text: str) -> bool:
    return any(low <= ord(c) <= high for c in text for low, high in _DEVANAGARI_RANGES)


def _devanagari_font(size: int, bold: bool) -> Font:
    if not shaping_available():
        raise ShortRenderError(SHAPING_HELP)
    for path, regular, heavy in _DEVANAGARI_FONTS:
        if Path(path).is_file():
            return _load(path, heavy if bold else regular, size)
    for regular_file, bold_file in _DEVANAGARI_FILES:
        path = bold_file if bold else regular_file
        if Path(path).is_file():
            return _load(path, 0, size)
    raise ShortRenderError(
        "No installed font can draw Hindi captions. Turn captions off for this Short."
    )


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
    """A font for ``text`` and a note on what was used (``''`` for the normal case).

    Raises :class:`ShortRenderError` for Devanagari text that cannot be drawn correctly.
    """
    if _is_devanagari(text):
        return _devanagari_font(size, bold), ""
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
    for font in (metrics.font(w) for w in words if _is_devanagari(w)):
        if isinstance(font, ImageFont.FreeTypeFont):  # marks above and below need more room
            ascent = max(ascent, font.getmetrics()[0])
            descent = max(descent, font.getmetrics()[1])
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
