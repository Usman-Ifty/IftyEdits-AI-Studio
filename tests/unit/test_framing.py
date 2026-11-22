"""Auto framing: which part of a wide picture the vertical window covers."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from iftyedits.adapters.framing import column_interest
from iftyedits.core.framing import CENTER, best_window, suggest_position

WINDOW = 0.3164  # 9:16 window over a 16:9 frame


def busy(width: int, lo: int, hi: int) -> list[float]:
    return [10.0 if lo <= i < hi else 0.1 for i in range(width)]


def test_window_goes_over_the_busy_area() -> None:
    left, contrast = best_window(busy(100, 5, 30), WINDOW)
    right, _ = best_window(busy(100, 70, 95), WINDOW)
    assert left < 0.2 < 0.8 < right
    assert contrast > 1.5


def test_flat_or_broken_input_is_centred() -> None:
    assert best_window([0.0] * 50, WINDOW) == (CENTER, 1.0)
    assert best_window([float("nan")] * 50, WINDOW)[0] == CENTER
    assert best_window([1.0], WINDOW) == (CENTER, 1.0)
    assert best_window([1.0] * 50, 1.0) == (CENTER, 1.0)


def test_even_picture_keeps_the_centre() -> None:
    result = suggest_position([[1.0] * 80] * 4, WINDOW)
    assert result.position == CENTER and result.method == "center"


def test_too_few_frames_fall_back() -> None:
    result = suggest_position([busy(100, 0, 20)], WINDOW)
    assert (result.position, result.method) == (CENTER, "none")


def test_one_steady_position_from_several_frames() -> None:
    frames = [busy(100, 60, 90)] * 4 + [busy(100, 0, 20)]  # one odd frame does not move it
    result = suggest_position(frames, WINDOW)
    assert result.method == "detail" and result.position > 0.7
    assert suggest_position(frames, WINDOW) == result  # deterministic


@pytest.mark.parametrize("side", ["left", "right"])
def test_picture_with_detail_on_one_side(side: str) -> None:
    image = Image.new("RGB", (640, 360), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    x0 = 40 if side == "left" else 460
    for i in range(0, 140, 14):
        draw.rectangle((x0 + i, 60, x0 + i + 6, 300), fill=(240, 240, 240))
    columns = column_interest(image)
    assert len(columns) == 640 // 360 * 144 or len(columns) > 200
    position = suggest_position([columns] * 3, WINDOW).position
    assert (position < 0.35) if side == "left" else (position > 0.65)


# ------------------------------------------------------------- following a subject

from iftyedits.adapters.framing import picture_change  # noqa: E402
from iftyedits.core.framing import (  # noqa: E402
    FramingObservation,
    follow_subject,
    position_at,
)


def obs(*points: tuple[float, float], cut_at: tuple[float, ...] = ()) -> list[FramingObservation]:
    return [FramingObservation(t, p, 1.0, t in cut_at) for t, p in points]


def test_steady_subject_gives_one_position() -> None:
    path = follow_subject(obs((1, 0.62), (3, 0.6), (5, 0.61), (7, 0.6)), 8.0)
    assert path.method == "steady" and not path.moves and len(path.keyframes) == 1
    assert 0.59 <= path.keyframes[0][1] <= 0.63


def test_small_drift_inside_the_dead_zone_does_not_move_the_window() -> None:
    path = follow_subject(obs((1, 0.5), (3, 0.55), (5, 0.45), (7, 0.52)), 8.0)
    assert not path.moves


def test_a_moving_subject_is_followed_slowly_never_jumping() -> None:
    path = follow_subject(obs((1, 0.1), (3, 0.1), (5, 0.9), (7, 0.9), (9, 0.9)), 10.0)
    assert path.method == "follow" and path.moves
    for (t0, p0), (t1, p1) in zip(path.keyframes, path.keyframes[1:], strict=False):
        assert t1 > t0
        assert abs(p1 - p0) / (t1 - t0) <= 0.12 + 1e-6  # the speed limit
    assert all(0.0 <= p <= 1.0 for _, p in path.keyframes)
    assert path.keyframes[0][0] == 0.0 and path.keyframes[-1][0] == 10.0


def test_a_single_odd_reading_is_ignored() -> None:
    path = follow_subject(obs((1, 0.3), (3, 0.3), (5, 0.95), (7, 0.3), (9, 0.3)), 10.0)
    assert not path.moves


def test_a_scene_cut_may_jump_at_once() -> None:
    path = follow_subject(
        obs((1, 0.2), (3, 0.2), (5, 0.8), (7, 0.8), (9, 0.8), cut_at=(5.0,)), 10.0
    )
    assert path.moves
    assert (
        max(abs(b[1] - a[1]) for a, b in zip(path.keyframes, path.keyframes[1:], strict=False))
        > 0.5
    )
    assert position_at(path.keyframes, 5.1) == pytest.approx(0.8, abs=0.01)


def test_weak_or_missing_readings_fall_back_to_the_centre() -> None:
    assert follow_subject([], 10.0).keyframes == ((0.0, CENTER),)
    weak = [FramingObservation(t, 0.9, 0.0) for t in (1, 3, 5)]
    assert follow_subject(weak, 10.0).method == "none"
    bad = [FramingObservation(float("nan"), 0.9), FramingObservation(2, float("inf"))]
    assert follow_subject(bad, 10.0).method == "none"
    assert follow_subject(obs((1, 0.2), (3, 0.9)), 0.0).method == "none"


def test_follow_is_deterministic_and_bounded() -> None:
    points = [(float(t), (t * 0.37) % 1.0) for t in range(1, 60, 2)]
    a = follow_subject(obs(*points), 60.0)
    assert a == follow_subject(obs(*points), 60.0)
    assert len(a.keyframes) <= 26


def test_position_at_interpolates_and_clamps_to_the_ends() -> None:
    path = ((0.0, 0.2), (4.0, 0.6))
    assert position_at(path, -1) == 0.2 and position_at(path, 9) == 0.6
    assert position_at(path, 2) == pytest.approx(0.4)
    assert position_at((), 1) == CENTER


def test_picture_change_tells_a_cut_from_a_nudge() -> None:
    a = Image.new("RGB", (320, 180), "black")
    b = Image.new("RGB", (320, 180), "white")
    c = a.copy()
    ImageDraw.Draw(c).rectangle((0, 0, 8, 8), fill="white")
    assert picture_change(a, b) > 0.9
    assert picture_change(a, c) < 0.02
    assert picture_change(a, a) == 0.0
