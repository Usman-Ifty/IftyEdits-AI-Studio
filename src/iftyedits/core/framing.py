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
from itertools import pairwise

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


# ---------------------------------------------------------------------- following a subject

DEAD_ZONE = 0.08
"""Subject drift (as a share of the sideways travel) that is ignored; the window stays put."""
MAX_SPEED = 0.12
"""Fastest window movement, in share of the travel per second (a slow, steady pan)."""
CUT_JUMP = 0.35
"""A jump this large between two samples, with a cut flagged, moves the window instantly."""
MIN_TRAVEL = 0.1
"""If the window would move less than this overall, one steady position is used instead."""
MAX_KEYFRAMES = 24


@dataclass(frozen=True, slots=True)
class FramingObservation:
    """Where the subject was at one sampled moment (seconds from the clip start)."""

    time_s: float
    position: float
    """Window position (0..1) that centres the subject at this moment."""
    weight: float = 1.0
    """Trust in this reading (0 = none). Weak readings are ignored."""
    cut: bool = False
    """True when the picture changed completely since the previous sample (a scene cut)."""


@dataclass(frozen=True, slots=True)
class CropPath:
    """A smooth window path as ``(seconds from clip start, position)`` keyframes."""

    keyframes: tuple[tuple[float, float], ...]
    method: str
    """``"follow"`` (moves), ``"steady"`` (one position), ``"center"`` or ``"none"``."""
    confidence: float
    note: str

    @property
    def moves(self) -> bool:
        """True when the window changes position during the clip."""
        return len({round(p, 3) for _, p in self.keyframes}) > 1


def follow_subject(
    observations: Sequence[FramingObservation],
    duration_s: float,
    *,
    dead_zone: float = DEAD_ZONE,
    max_speed: float = MAX_SPEED,
    min_weight: float = 0.05,
) -> CropPath:
    """Turn noisy per-moment subject positions into a calm window path.

    Rules: readings are smoothed with a median of three; the window holds still while the
    subject stays inside the dead zone; when it leaves, the window moves toward it at no
    more than ``max_speed`` (so there are no jumps), except across a scene cut where it may
    jump at once; if the path would barely move, one steady position is returned. Fewer than
    two usable readings return the centre. The path is deterministic.
    """
    usable = sorted(
        (
            o
            for o in observations
            if math.isfinite(o.time_s) and math.isfinite(o.position) and o.weight >= min_weight
        ),
        key=lambda o: o.time_s,
    )
    if duration_s <= 0 or len(usable) < MIN_SAMPLES:
        return CropPath(((0.0, CENTER),), "none", 0.0, "Too few readings; centred.")
    raw = [min(1.0, max(0.0, o.position)) for o in usable]
    smooth = list(raw)
    for i in range(len(raw)):
        if usable[i].cut:
            continue
        lo = i - 1 if i > 0 and not usable[i].cut else i
        hi = i + 1 if i + 1 < len(raw) and not usable[i + 1].cut else i
        smooth[i] = statistics.median(raw[lo : hi + 1])
    held = smooth[0]
    keys: list[tuple[float, float]] = [(0.0, round(held, 3))]
    last_t = 0.0
    for obs, target in zip(usable, smooth, strict=True):
        t = min(max(obs.time_s, 0.0), duration_s)
        dt = max(0.0, t - last_t)
        if obs.cut and abs(target - held) >= CUT_JUMP:
            if t > last_t:
                keys.append((round(max(last_t, t - 0.001), 3), round(held, 3)))
            held = target
        elif abs(target - held) > dead_zone:
            step = max_speed * dt
            held = held + max(-step, min(step, target - held))
        if t > last_t or keys[-1][1] != round(held, 3):
            keys.append((round(t, 3), round(held, 3)))
            last_t = max(last_t, t)
    if last_t < duration_s:
        keys.append((round(duration_s, 3), round(held, 3)))
    positions = [p for _, p in keys]
    confidence = round(sum(o.weight for o in usable) / len(usable), 2)
    confidence = max(0.0, min(1.0, confidence))
    if max(positions) - min(positions) < MIN_TRAVEL:
        steady = round(statistics.median(smooth), 3)
        return CropPath(
            ((0.0, steady),),
            "steady",
            confidence,
            "The subject stays put, so the window is steady.",
        )
    return CropPath(
        _thin(keys), "follow", confidence, "The window follows the subject with a slow, smooth pan."
    )


def _thin(keys: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Drop repeated times and cap the keyframe count (keeps the first and last)."""
    clean: list[tuple[float, float]] = []
    for t, p in keys:
        if clean and t <= clean[-1][0]:
            clean[-1] = (clean[-1][0], p) if t == clean[-1][0] else clean[-1]
            continue
        if len(clean) >= 2 and clean[-1][1] == p == clean[-2][1]:
            clean[-1] = (t, p)
            continue
        clean.append((t, p))
    if len(clean) > MAX_KEYFRAMES:
        step = (len(clean) - 1) / (MAX_KEYFRAMES - 1)
        clean = [clean[round(i * step)] for i in range(MAX_KEYFRAMES)]
    return tuple(clean)


def position_at(path: Sequence[tuple[float, float]], t: float) -> float:
    """Window position at ``t`` seconds by linear interpolation between keyframes."""
    if not path:
        return CENTER
    if t <= path[0][0]:
        return path[0][1]
    for (t0, p0), (t1, p1) in pairwise(path):
        if t <= t1:
            return p0 if t1 <= t0 else p0 + (p1 - p0) * (t - t0) / (t1 - t0)
    return path[-1][1]
