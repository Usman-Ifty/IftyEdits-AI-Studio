"""Scene-change detection with FFmpeg's ``select`` scene score on the proxy video.

The proxy (720p short side, at most 30 fps) is decoded once at a reduced frame rate and
size; FFmpeg writes the frames whose scene score exceeds the threshold, with their
times, to a temporary metadata file inside ``data/derived``. No new dependency is
needed (see docs/DECISIONS.md D20).
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from iftyedits.core.clip_models import SceneCut
from iftyedits.core.errors import ClipAnalysisError
from iftyedits.core.protocols import CancelToken, MediaTranscoder

ANALYSIS_FPS = 10
ANALYSIS_WIDTH = 320
MIN_CUT_GAP_S = 0.5
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")

PathGuard = Callable[[Path], Path]


def scene_filter(threshold: float, metadata_file: Path) -> str:
    """Filter graph that logs frames whose scene score is above ``threshold``."""
    target = str(metadata_file)
    if any(c in target for c in "'\\\n"):
        raise ClipAnalysisError("The analysis folder path contains unsupported characters")
    return (
        f"fps={ANALYSIS_FPS},scale={ANALYSIS_WIDTH}:-2,"
        f"select='gt(scene,{threshold:.3f})',"
        f"metadata=mode=print:file='{target}'"
    )


def parse_scene_metadata(text: str, *, min_gap_s: float = MIN_CUT_GAP_S) -> list[SceneCut]:
    """Scene cuts from ``metadata=mode=print`` output (one ``frame:`` line per cut)."""
    cuts: list[SceneCut] = []
    time_s: float | None = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            time_s = float(pts.group(1))
            continue
        score = _SCORE_RE.search(line)
        if score and time_s is not None:
            value = min(1.0, max(0.0, float(score.group(1))))
            if cuts and time_s - cuts[-1].time_s < min_gap_s:
                if value > cuts[-1].score:
                    cuts[-1] = SceneCut(round(cuts[-1].time_s, 3), round(value, 4))
            else:
                cuts.append(SceneCut(round(time_s, 3), round(value, 4)))
            time_s = None
    return cuts


class FFmpegSceneDetector:
    """Implements :class:`iftyedits.core.protocols.SceneDetector`."""

    def __init__(self, transcoder: MediaTranscoder, work_root: Path, guard: PathGuard) -> None:
        self.transcoder = transcoder
        self.work_root = work_root
        self.guard = guard

    def detect(
        self,
        video: Path,
        *,
        threshold: float,
        duration_s: float | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> list[SceneCut]:
        """Scene cuts in ``video`` (seconds from its start)."""
        root = self.guard(self.work_root)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".scenes-", dir=root) as tmp:
            metadata = self.guard(Path(tmp) / "scenes.txt")

            def on_seconds(seconds: float) -> None:
                if progress is not None and duration_s:
                    progress(min(1.0, seconds / duration_s))

            self.transcoder.run(
                ["-i", str(video), "-an", "-sn", "-dn",
                 "-vf", scene_filter(threshold, metadata), "-f", "null", "-"],
                progress=on_seconds,
                cancel=cancel,
                redact=(video, metadata),
            )  # fmt: skip
            if not metadata.exists():
                return []
            return parse_scene_metadata(metadata.read_text(encoding="utf-8", errors="replace"))
