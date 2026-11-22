"""Measure how much visual detail each column of a picture holds (for auto framing)."""

from __future__ import annotations

import numpy as np
from PIL import Image

ANALYSIS_HEIGHT = 144


def column_interest(image: Image.Image) -> list[float]:
    """Edge strength summed down each column of a downscaled grayscale copy.

    Flat areas (sky, a plain wall, letterbox bars) score near zero; faces, text and busy
    scenes score high. Deterministic: no randomness and no model.
    """
    gray = image.convert("L")
    scale = ANALYSIS_HEIGHT / gray.height
    small = gray.resize((max(2, round(gray.width * scale)), ANALYSIS_HEIGHT), Image.Resampling.BOX)
    data = np.asarray(small, dtype=np.float32)
    dx = np.abs(np.diff(data, axis=1))
    dy = np.abs(np.diff(data, axis=0))
    energy = dx[:-1, :] + dy[:, :-1]
    columns = energy.sum(axis=0)
    columns = np.append(columns, columns[-1])  # restore the width lost to differencing
    return [float(v) for v in columns]
