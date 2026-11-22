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
