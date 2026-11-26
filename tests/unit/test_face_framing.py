"""Face-aware framing: choosing the window, decoding the model and following shots.

No real model and no video: faces and pictures are generated.
"""

from __future__ import annotations

import hashlib
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from iftyedits.adapters import model_store
from iftyedits.adapters.faces import (
    INPUT_SIZE,
    STRIDES,
    YuNetFaceDetector,
    decode,
    letterbox,
    load_face_detector,
    suppress,
)
from iftyedits.adapters.model_store import FACE_MODEL, build_face_model_store
from iftyedits.cli import main
from iftyedits.config import Settings
from iftyedits.core.errors import FaceDetectionError
from iftyedits.core.framing import (
    MAX_KEYFRAMES,
    FaceBox,
    FramingObservation,
    face_position,
    follow_subject,
    position_at,
)
from iftyedits.core.models import ModelFile, SpeechModelSpec
from iftyedits.core.shorts import MAX_PATH_KEYFRAMES, ShortConfig
from iftyedits.services.shorts import ShortService, shot_samples

WINDOW = 0.3164  # a 9:16 window of a 16:9 picture


def face(centre: float, height: float = 0.2, score: float = 0.9) -> FaceBox:
    width = height * 9 / 16
    return FaceBox(centre - width / 2, 0.2, width, height, score)


def window_centre(position: float) -> float:
    return position * (1 - WINDOW) + WINDOW / 2


# ---------------------------------------------------------------------- choosing the window


def test_one_face_is_centred() -> None:
    found = face_position([face(0.6)], WINDOW)
    assert found is not None
    assert window_centre(found[0]) == pytest.approx(0.6)
    assert found[1] == pytest.approx(0.9)


def test_faces_at_the_edges_keep_the_window_inside_the_picture() -> None:
    left = face_position([face(0.03)], WINDOW)
    right = face_position([face(0.97)], WINDOW)
    assert left is not None and right is not None
    assert (left[0], right[0]) == (0.0, 1.0)


def test_no_faces_or_only_tiny_faces_give_nothing() -> None:
    assert face_position([], WINDOW) is None
    assert face_position([face(0.5, height=0.04)], WINDOW) is None
    assert face_position([face(0.5)], 1.0) is None


def test_two_close_faces_are_framed_together() -> None:
    found = face_position([face(0.45, 0.15), face(0.58, 0.15)], WINDOW)
    assert found is not None
    assert window_centre(found[0]) == pytest.approx(0.515)


def test_the_larger_of_two_distant_faces_wins_without_history() -> None:
    found = face_position([face(0.2, 0.15), face(0.8, 0.3)], WINDOW)
    assert found is not None
    assert window_centre(found[0]) == pytest.approx(0.8)


def test_the_current_face_is_kept_unless_another_is_clearly_bigger() -> None:
    held = face_position([face(0.25)], WINDOW)
    assert held is not None
    similar = face_position([face(0.25, 0.2), face(0.8, 0.21)], WINDOW, held[0])
    assert similar is not None
    assert window_centre(similar[0]) == pytest.approx(0.25)
    bigger = face_position([face(0.25, 0.2), face(0.75, 0.4)], WINDOW, held[0])
    assert bigger is not None
    assert window_centre(bigger[0]) == pytest.approx(0.75)


# ---------------------------------------------------------------------- the path


def test_a_cut_jumps_at_once_even_for_a_small_change() -> None:
    readings = [
        FramingObservation(0.0, 0.3),
        FramingObservation(2.0, 0.3),
        FramingObservation(4.0, 0.5, cut=True),
        FramingObservation(5.0, 0.5),
    ]
    path = follow_subject(readings, 6.0, cut_jump=0.08)
    assert position_at(path.keyframes, 3.99) == pytest.approx(0.3, abs=0.01)
    assert position_at(path.keyframes, 4.0) == pytest.approx(0.5)


def test_thinning_a_long_path_keeps_every_jump() -> None:
    readings: list[FramingObservation] = []
    for shot in range(10):
        position = 0.2 if shot % 2 == 0 else 0.8
        for step in range(5):
            time_s = shot * 5.0 + step
            readings.append(FramingObservation(time_s, position, cut=step == 0 and shot > 0))
    path = follow_subject(readings, 50.0, cut_jump=0.08)
    assert len(path.keyframes) <= MAX_KEYFRAMES <= MAX_PATH_KEYFRAMES
    for shot in range(10):
        expected = 0.2 if shot % 2 == 0 else 0.8
        assert position_at(path.keyframes, shot * 5.0 + 0.01) == pytest.approx(expected, abs=0.01)
        assert position_at(path.keyframes, shot * 5.0 + 4.9) == pytest.approx(expected, abs=0.01)
    config = ShortConfig(start_s=0.0, end_s=50.0, crop_path=path.keyframes)
    assert config.crop_path == path.keyframes


# ---------------------------------------------------------------------- sampling


def test_shots_are_sampled_after_each_cut_and_dated_at_the_cut() -> None:
    samples = shot_samples([4.0], 7.0)
    starts = [(t, at) for t, at, first in samples if first]
    assert [t for t, _ in starts] == [0.0, 4.0]
    assert [at for _, at in starts] == [pytest.approx(0.1), pytest.approx(4.1)]
    assert all(0.0 <= at < 7.0 for _, at, _ in samples)
    times = [t for t, _, _ in samples]
    assert all(a < b for a, b in pairwise(times))


def test_sampling_is_capped_and_survives_very_short_shots() -> None:
    assert len(shot_samples([], 600.0, max_samples=20)) <= 20
    assert len(shot_samples([i * 0.5 for i in range(1, 100)], 50.0, max_samples=30)) == 30
    assert [first for _, _, first in shot_samples([1.0, 1.05], 2.0)] == [True, True, True]


# ---------------------------------------------------------------------- the model adapter


def outputs_with(stride: int, row: int, col: int, score: float) -> dict[str, np.ndarray]:
    outputs: dict[str, np.ndarray] = {}
    for s in STRIDES:
        cells = (INPUT_SIZE // s) ** 2
        outputs[f"cls_{s}"] = np.zeros((1, cells, 1), dtype=np.float32)
        outputs[f"obj_{s}"] = np.zeros((1, cells, 1), dtype=np.float32)
        outputs[f"bbox_{s}"] = np.zeros((1, cells, 4), dtype=np.float32)
    index = row * (INPUT_SIZE // stride) + col
    outputs[f"cls_{stride}"][0, index, 0] = score
    outputs[f"obj_{stride}"][0, index, 0] = score
    outputs[f"bbox_{stride}"][0, index] = (0.0, 0.0, np.log(4.0), np.log(5.0))
    return outputs


def test_decode_places_a_face_as_fractions_of_the_picture() -> None:
    found = decode(outputs_with(16, 10, 20, 0.9), (640, 360))
    assert len(found) == 1
    box = found[0]
    assert box.center_x == pytest.approx(20 * 16 / 640)
    assert box.y + box.height / 2 == pytest.approx(10 * 16 / 360)
    assert box.width == pytest.approx(64 / 640)
    assert box.height == pytest.approx(80 / 360)
    assert box.score == pytest.approx(0.9)


def test_decode_drops_weak_scores_and_boxes_outside_the_picture() -> None:
    assert decode(outputs_with(16, 10, 20, 0.5), (640, 360)) == []
    assert decode(outputs_with(16, 38, 20, 0.9), (640, 360)) == []  # in the padding


def test_overlapping_boxes_are_merged() -> None:
    best, near, far = face(0.5, 0.2, 0.95), face(0.502, 0.2, 0.8), face(0.8, 0.2, 0.7)
    assert suppress([near, far, best]) == [best, far]


def test_letterbox_keeps_the_shape_and_uses_bgr() -> None:
    tensor, scale, size = letterbox(Image.new("RGB", (1280, 720), (255, 10, 20)))
    assert tensor.shape == (1, 3, INPUT_SIZE, INPUT_SIZE)
    assert (scale, size) == (0.5, (640, 360))
    assert list(tensor[0, :, 0, 0]) == [20.0, 10.0, 255.0]
    assert float(tensor[0, :, 500, 0].sum()) == 0.0


def test_a_damaged_model_is_reported_not_raised_raw(tmp_path: Path) -> None:
    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"not a model")
    picture = tmp_path / "p.png"
    Image.new("RGB", (64, 36)).save(picture)
    with pytest.raises(FaceDetectionError, match="face model"):
        YuNetFaceDetector(broken).detect(picture)


def fake_catalog(body: bytes) -> tuple[SpeechModelSpec, ...]:
    return (
        SpeechModelSpec(
            name=FACE_MODEL,
            repo_id="opencv/face_detection_yunet",
            revision="a" * 40,
            files=(ModelFile("m.onnx", len(body), hashlib.sha256(body).hexdigest()),),
            summary="test",
            ram_gb=0.1,
        ),
    )


def test_the_detector_needs_a_verified_model_and_the_setting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_face_detector(settings) is None  # not downloaded
    monkeypatch.setattr(model_store, "FACE_CATALOG", fake_catalog(b"weights"))
    folder = build_face_model_store(settings).path(FACE_MODEL)
    folder.mkdir(parents=True)
    (folder / "m.onnx").write_bytes(b"weights")
    assert load_face_detector(settings) is not None
    assert load_face_detector(settings.model_copy(update={"face_framing": False})) is None
    (folder / "m.onnx").write_bytes(b"tamperd")  # same size, wrong hash
    assert load_face_detector(settings) is None


def test_the_cli_lists_and_verifies_the_face_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IFTYEDITS_HOME", str(tmp_path))
    monkeypatch.delenv("IFTYEDITS_MODELS_DIR", raising=False)
    assert main(["models"]) == 0
    assert FACE_MODEL in capsys.readouterr().out
    assert main(["models", "verify", FACE_MODEL]) != 0


# ---------------------------------------------------------------------- the service


class FakeDetector:
    """Returns the faces listed for a picture's file name."""

    def __init__(self, faces: dict[str, list[FaceBox]], error: bool = False) -> None:
        self.faces = faces
        self.error = error

    def detect(self, image: Path) -> list[FaceBox]:
        if self.error:
            raise FaceDetectionError("broken")
        return self.faces.get(image.name, [])


def follow_service(
    tmp_path: Path,
    faces_at: dict[float, list[FaceBox]],
    cuts: list[float],
    *,
    error: bool = False,
) -> ShortService:
    """A service whose pictures are flat images named by their time."""
    service = object.__new__(ShortService)

    def still(video_id: int, at_s: float) -> Path:
        path = tmp_path / f"{at_s:.1f}.png"
        if not path.is_file():
            Image.new("RGB", (160, 90), (40, 40, 40)).save(path)
        return path

    def faces_for(at_s: float) -> list[FaceBox]:
        past = [t for t in faces_at if t <= at_s]
        return faces_at[max(past)] if past else []

    names = {f"{i / 10:.1f}.png": faces_for(i / 10) for i in range(600)}
    service.faces = FakeDetector(names, error)
    service.still = still  # type: ignore[method-assign]
    service._cut_times = lambda video_id, start_s, end_s: cuts  # type: ignore[method-assign]
    return service


def test_the_window_follows_faces_across_a_cut(tmp_path: Path) -> None:
    service = follow_service(tmp_path, {0.0: [face(0.25)], 5.0: [face(0.75)]}, [5.0])
    path = service._face_path(1, ShortConfig(start_s=0.0, end_s=10.0), WINDOW)
    assert path is not None and path.method == "follow"
    assert window_centre(position_at(path.keyframes, 4.9)) == pytest.approx(0.25, abs=0.01)
    assert window_centre(position_at(path.keyframes, 5.0)) == pytest.approx(0.75, abs=0.01)
    assert "faces" in path.note


def test_a_clip_with_few_faces_falls_back_to_picture_detail(tmp_path: Path) -> None:
    service = follow_service(tmp_path, {0.0: [face(0.25)], 1.0: []}, [])
    assert service._face_path(1, ShortConfig(start_s=0.0, end_s=10.0), WINDOW) is None


def test_a_missed_detection_inside_a_shot_holds_the_window(tmp_path: Path) -> None:
    service = follow_service(tmp_path, {0.0: [face(0.75)], 3.0: [], 5.0: [face(0.75)]}, [])
    path = service._face_path(1, ShortConfig(start_s=0.0, end_s=10.0), WINDOW)
    assert path is not None and path.method == "steady"
    assert window_centre(path.keyframes[0][1]) == pytest.approx(0.75, abs=0.01)


def test_a_shot_without_faces_is_not_framed_on_the_previous_face(tmp_path: Path) -> None:
    service = follow_service(tmp_path, {0.0: [face(0.15)], 6.0: []}, [6.0])
    path = service._face_path(1, ShortConfig(start_s=0.0, end_s=10.0), WINDOW)
    assert path is not None
    assert position_at(path.keyframes, 8.0) == pytest.approx(0.5, abs=0.01)  # flat picture


def test_a_detector_failure_or_missing_detector_falls_back(tmp_path: Path) -> None:
    config = ShortConfig(start_s=0.0, end_s=10.0)
    broken = follow_service(tmp_path, {0.0: [face(0.5)]}, [], error=True)
    assert broken._face_path(1, config, WINDOW) is None
    broken.faces = None
    assert broken._face_path(1, config, WINDOW) is None
