"""Choose where the 9:16 window sits in a wide frame (pure functions, no I/O).

The picture is judged by *visual interest per column*: edges and detail, which is where
faces, text, game action and slide content are. This is a plain heuristic. It does not
recognise faces or people and it can be wrong, so the result is only ever a starting
position the user can change, and an unclear picture falls back to the centre.

The window position is one steady value for the whole clip, taken as the median over
several sampled frames, so the picture never jumps between subjects.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

CENTER = 0.5
MIN_CONTRAST = 1.25
"""The best window must hold this many times the average interest of a window, else the
picture has no clear subject and the centre is kept."""
MIN_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class CropSuggestion:
    """A proposed window position (0 = left edge, 1 = right edge) and how it was chosen."""

    position: float
    method: str
    """``"detail"`` (found a busier area), ``"center"`` (nothing stood out) or ``"none"``."""
    confidence: float
    note: str


def best_window(columns: Sequence[float], window_frac: float) -> tuple[float, float]:
    """``(position, contrast)`` of the busiest window covering ``window_frac`` of the width.

    ``position`` is 0..1 as ``crop_position`` expects. ``contrast`` is the busiest window's
    interest divided by the average window's (1.0 means the frame is evenly busy).
    """
    width = len(columns)
    window = max(1, min(width, round(width * window_frac)))
    if width < 2 or window >= width:
        return CENTER, 1.0
    clean = [c if math.isfinite(c) and c > 0 else 0.0 for c in columns]
    total = sum(clean)
    if total <= 0.0:
        return CENTER, 1.0
    running = sum(clean[:window])
    sums = [running]
    for index in range(window, width):
        running += clean[index] - clean[index - window]
        sums.append(running)
    mean = sum(sums) / len(sums)
    best = max(range(len(sums)), key=lambda i: (sums[i], -abs(i - (len(sums) - 1) / 2)))
    contrast = sums[best] / mean if mean > 0 else 1.0
    return best / (len(sums) - 1), contrast


def suggest_position(
    frames: Sequence[Sequence[float]], window_frac: float, *, min_contrast: float = MIN_CONTRAST
) -> CropSuggestion:
    """Combine the column interest of several frames into one steady crop position."""
    if len(frames) < MIN_SAMPLES:
        return CropSuggestion(CENTER, "none", 0.0, "Too few frames could be read; centred.")
    picks = [best_window(f, window_frac) for f in frames]
    position = statistics.median(p for p, _ in picks)
    contrast = statistics.median(c for _, c in picks)
    spread = max(p for p, _ in picks) - min(p for p, _ in picks)
    if contrast < min_contrast:
        return CropSuggestion(
            CENTER, "center", 0.0, "Nothing in the picture stood out, so it is centred."
        )
    confidence = max(0.0, min(1.0, (contrast - 1.0) / 2.0)) * (1.0 - min(1.0, spread))
    note = (
        "Placed over the busiest part of the picture."
        if spread < 0.35
        else "The busy area moves around, so this is a compromise; check the crop guide."
    )
    return CropSuggestion(round(position, 3), "detail", round(confidence, 2), note)
