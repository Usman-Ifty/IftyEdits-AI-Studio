"""Local face detection with the YuNet ONNX model, run by ONNX Runtime on the CPU.

The model (OpenCV Zoo, MIT licence) only says where faces are in a picture. Nothing about
who they are is computed or stored. The weights are downloaded only on request
(``iftyedits models download yunet``), pinned to one revision and checked against their
SHA-256; without them the studio frames clips from picture detail instead.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from iftyedits.adapters.model_store import FACE_MODEL, build_face_model_store
from iftyedits.config import Settings
from iftyedits.core.errors import FaceDetectionError
from iftyedits.core.framing import FaceBox

logger = logging.getLogger(__name__)

INPUT_SIZE = 640
STRIDES = (8, 16, 32)
SCORE_THRESHOLD = 0.75
NMS_THRESHOLD = 0.3

Array = NDArray[np.float32]


def letterbox(image: Image.Image) -> tuple[Array, float, tuple[int, int]]:
    """The model input (1x3x640x640, BGR) plus the scale and size of the picture inside it."""
    scale = INPUT_SIZE / max(image.width, image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    small = image.convert("RGB").resize(size, Image.Resampling.BILINEAR)
    canvas = np.zeros((INPUT_SIZE, INPUT_SIZE, 3), dtype=np.float32)
    canvas[: size[1], : size[0]] = np.asarray(small, dtype=np.float32)[:, :, ::-1]
    return canvas.transpose(2, 0, 1)[np.newaxis], scale, size


def decode(
    outputs: Mapping[str, Array], size: tuple[int, int], score_threshold: float = SCORE_THRESHOLD
) -> list[FaceBox]:
    """Faces from the raw YuNet heads, as fractions of the picture of ``size`` pixels."""
    boxes: list[FaceBox] = []
    width, height = size
    for stride in STRIDES:
        cols = INPUT_SIZE // stride
        cls = np.clip(outputs[f"cls_{stride}"].reshape(-1), 0.0, 1.0)
        obj = np.clip(outputs[f"obj_{stride}"].reshape(-1), 0.0, 1.0)
        bbox = outputs[f"bbox_{stride}"].reshape(-1, 4)
        scores = np.sqrt(cls * obj)
        for index in np.flatnonzero(scores >= score_threshold):
            row, col = divmod(int(index), cols)
            dx, dy, dw, dh = (float(v) for v in bbox[index])
            if not all(math.isfinite(v) for v in (dx, dy, dw, dh)):
                continue
            w = math.exp(min(dw, 10.0)) * stride
            h = math.exp(min(dh, 10.0)) * stride
            x0 = max(0.0, (col + dx) * stride - w / 2)
            y0 = max(0.0, (row + dy) * stride - h / 2)
            x1 = min(float(width), (col + dx) * stride + w / 2)
            y1 = min(float(height), (row + dy) * stride + h / 2)
            if x1 <= x0 or y1 <= y0:
                continue
            boxes.append(
                FaceBox(
                    x0 / width,
                    y0 / height,
                    (x1 - x0) / width,
                    (y1 - y0) / height,
                    float(scores[index]),
                )
            )
    return suppress(boxes)


def _overlap(a: FaceBox, b: FaceBox) -> float:
    """Intersection over union of two boxes."""
    w = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    h = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a.width * a.height + b.width * b.height - inter)


def suppress(boxes: Sequence[FaceBox], threshold: float = NMS_THRESHOLD) -> list[FaceBox]:
    """Non-maximum suppression: keep the best of each group of overlapping boxes."""
    kept: list[FaceBox] = []
    for box in sorted(boxes, key=lambda b: (-b.score, b.x, b.y)):
        if all(_overlap(box, other) <= threshold for other in kept):
            kept.append(box)
    return kept


class YuNetFaceDetector:
    """Implements :class:`iftyedits.core.protocols.FaceDetector`."""

    def __init__(self, model: Path, *, score_threshold: float = SCORE_THRESHOLD) -> None:
        self.model = model
        self.score_threshold = score_threshold
        self._session: Any = None

    def _load(self) -> Any:
        if self._session is None:
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.log_severity_level = 3
            try:
                self._session = onnxruntime.InferenceSession(
                    str(self.model), sess_options=options, providers=["CPUExecutionProvider"]
                )
            except Exception as exc:  # onnxruntime raises its own undocumented types
                raise FaceDetectionError(f"Could not load the face model: {exc}") from exc
        return self._session

    def detect(self, image: Path) -> list[FaceBox]:
        """Faces in a picture file."""
        session = self._load()
        try:
            with Image.open(image) as picture:
                tensor, _, size = letterbox(picture)
        except OSError as exc:
            raise FaceDetectionError(f"Could not read the picture {image.name}: {exc}") from exc
        try:
            names = [o.name for o in session.get_outputs()]
            values = session.run(names, {session.get_inputs()[0].name: tensor})
            return decode(dict(zip(names, values, strict=True)), size, self.score_threshold)
        except Exception as exc:
            raise FaceDetectionError(f"Face detection failed: {exc}") from exc


def load_face_detector(settings: Settings) -> YuNetFaceDetector | None:
    """The detector when face framing is on and the verified model is installed, else None."""
    if not settings.face_framing:
        return None
    store = build_face_model_store(settings)
    if not store.verify(FACE_MODEL):
        return None
    spec = store.spec(FACE_MODEL)
    return YuNetFaceDetector(store.path(FACE_MODEL) / spec.files[0].name)
