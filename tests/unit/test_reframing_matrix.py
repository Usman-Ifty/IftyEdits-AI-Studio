"""Reframing matrix: subjects, cuts, edges and source shapes (Phase 4C, synthetic readings)."""

from __future__ import annotations

import itertools

import pytest

from iftyedits.core.framing import (
    CENTER,
    CUT_JUMP,
    MAX_SPEED,
    FramingObservation,
    follow_subject,
    position_at,
)
from iftyedits.core.shorts import compute_crop


def readings(
    points: list[tuple[float, float]], cuts: tuple[float, ...] = ()
) -> list[FramingObservation]:
    return [FramingObservation(t, p, 1.0, t in cuts) for t, p in points]


def sampled(duration: float, fn: object, step: float = 1.0) -> list[tuple[float, float]]:
    assert callable(fn)
    return [(t * step, float(fn(t * step))) for t in range(1, int(duration / step))]


def travel(keys: tuple[tuple[float, float], ...]) -> float:
    return sum(abs(b[1] - a[1]) for a, b in itertools.pairwise(keys))


def max_speed(keys: tuple[tuple[float, float], ...]) -> float:
    return max(
        (abs(b[1] - a[1]) / (b[0] - a[0]) for a, b in itertools.pairwise(keys) if b[0] > a[0]),
        default=0.0,
    )


def test_static_talking_head_with_sensor_noise_is_perfectly_still() -> None:
    noise = [0.012, -0.02, 0.018, -0.01, 0.015, -0.017, 0.009, -0.011]
    pts = [(float(i + 1), 0.55 + n) for i, n in enumerate(noise)]
    path = follow_subject(readings(pts), 10.0)
    assert not path.moves and path.method == "steady"


def test_subject_pacing_left_and_right_is_followed_without_shaking() -> None:
    pts = sampled(60, lambda t: 0.5 + 0.35 * (1 if int(t // 10) % 2 == 0 else -1), step=2.0)
    path = follow_subject(readings(pts), 60.0)
    assert path.moves
    assert max_speed(path.keyframes) <= MAX_SPEED + 1e-6
    reversals = sum(
        1
        for a, b, c in itertools.zip_longest(path.keyframes, path.keyframes[1:], path.keyframes[2:])
        if b and c and (b[1] - a[1]) * (c[1] - b[1]) < -1e-9
    )
    assert reversals <= 6  # turns around when the subject does, not on every reading


def test_two_people_alternating_readings_do_not_make_the_window_swing() -> None:
    """Speaker A at 0.25, speaker B at 0.75, readings flip every sample."""
    pts = [(float(i + 1), 0.25 if i % 2 == 0 else 0.75) for i in range(30)]
    path = follow_subject(readings(pts), 31.0)
    # the window may settle somewhere, but must not sweep back and forth
    assert travel(path.keyframes) <= 1.0, path.keyframes


def test_two_people_taking_turns_in_blocks_follows_with_a_slow_pan() -> None:
    pts = [(float(i + 1), 0.25 if (i // 8) % 2 == 0 else 0.75) for i in range(32)]
    path = follow_subject(readings(pts), 33.0)
    assert max_speed(path.keyframes) <= MAX_SPEED + 1e-6


def test_rapid_scene_cuts_jump_only_on_the_cuts() -> None:
    cuts = (4.0, 8.0, 12.0, 16.0)
    levels = {
        1.0: 0.2,
        3.0: 0.2,
        4.0: 0.8,
        6.0: 0.8,
        8.0: 0.3,
        10.0: 0.3,
        12.0: 0.7,
        14.0: 0.7,
        16.0: 0.2,
        18.0: 0.2,
    }
    path = follow_subject(readings(sorted(levels.items()), cuts), 20.0)
    jumps = [
        (a[0], b[0]) for a, b in itertools.pairwise(path.keyframes) if abs(b[1] - a[1]) >= CUT_JUMP
    ]
    assert len(jumps) <= len(cuts)
    for t0, t1 in jumps:
        assert any(t0 <= c <= t1 + 0.01 for c in cuts) and t1 - t0 <= 0.01


def test_no_subject_anywhere_gives_the_centre() -> None:
    weak = [FramingObservation(float(t), 0.9, 0.0) for t in range(1, 9)]
    path = follow_subject(weak, 10.0)
    assert path.method == "none" and path.keyframes == ((0.0, CENTER),)


@pytest.mark.parametrize("edge", [0.0, 1.0, -0.4, 1.7])
def test_subject_at_or_beyond_the_frame_edge_stays_in_bounds(edge: float) -> None:
    pts = [(float(t), edge) for t in range(1, 12)]
    path = follow_subject(readings(pts), 12.0)
    assert all(0.0 <= p <= 1.0 for _, p in path.keyframes)
    assert all(0.0 <= position_at(path.keyframes, t / 4) <= 1.0 for t in range(0, 48))


def test_late_reaction_is_bounded_by_the_speed_limit() -> None:
    """A subject jumping across the frame is reached within distance / max_speed seconds."""
    pts = [(float(t), 0.15) for t in range(1, 6)] + [(float(t), 0.85) for t in range(6, 40)]
    path = follow_subject(readings(pts), 40.0)
    needed = 0.7 / MAX_SPEED
    assert position_at(path.keyframes, 5.0 + needed + 6.0) >= 0.8  # arrives, with slack


SOURCES = [
    (1920, 1080),  # landscape
    (3840, 2160),  # 4K
    (1280, 720),
    (640, 360),  # low resolution
    (1080, 1080),  # square
    (1080, 1920),  # already portrait
    (720, 1280),
    (2560, 1080),  # ultrawide
    (3440, 1440),
    (1000, 1000),
    (1920, 800),  # cinemascope
    (854, 480),
    (1081, 1921),  # odd dimensions
    (4, 4),  # tiny
]


@pytest.mark.parametrize("size", SOURCES)
@pytest.mark.parametrize("position", [-1.0, 0.0, 0.3, 0.5, 1.0, 2.0, float("nan")])
def test_every_crop_is_inside_the_frame_even_sized_and_9_16(
    size: tuple[int, int], position: float
) -> None:
    w, h = size
    crop = compute_crop(w, h, position)
    assert crop.x >= 0 and crop.x + crop.width <= w
    assert crop.y >= 0 and crop.y + crop.height <= h
    assert crop.width % 2 == 0 and crop.height % 2 == 0
    if size != (4, 4):
        assert crop.aspect_error < 0.01  # no visible stretch
    assert (crop.out_w, crop.out_h) == (1080, 1920)


def test_portrait_source_is_not_cut() -> None:
    crop = compute_crop(1080, 1920, 0.2)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1080, 1920)


def test_window_position_moves_the_crop_across_a_wide_source() -> None:
    xs = [compute_crop(1920, 1080, p).x for p in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert xs == sorted(xs) and xs[0] == 0 and xs[-1] + 606 <= 1920


def test_a_small_subject_is_centred_in_the_window_not_left_on_its_edge() -> None:
    from iftyedits.core.framing import best_window

    columns = [0.0] * 100
    for i in range(60, 66):  # a small subject at 60-65 % of the width
        columns[i] = 5.0
    position, _ = best_window(columns, 0.3)  # window is 30 columns wide
    window_left = position * (100 - 30)
    assert abs((window_left + 15) - 63) <= 2  # window centre sits on the subject
