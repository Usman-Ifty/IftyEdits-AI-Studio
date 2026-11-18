"""ffprobe adapter: runs ffprobe read-only and parses its JSON output."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from iftyedits.core.errors import ExternalToolError, ProbeError
from iftyedits.core.models import MediaInfo, StreamInfo

_MAX_STDERR = 500


class FFprobe:
    """Implements :class:`iftyedits.core.protocols.MediaProber` with ffprobe."""

    def __init__(self, binary: str = "ffprobe", timeout_s: float = 60.0) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise ExternalToolError(f"ffprobe not found: {binary!r}")
        self.binary = resolved
        self.timeout_s = timeout_s

    def probe(self, path: Path) -> MediaInfo:
        """Probe ``path``. The file is only read."""
        target = path.resolve(strict=True)
        args = [
            self.binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-i",
            str(target),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args, capture_output=True, text=True, timeout=self.timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalToolError(f"ffprobe timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise ExternalToolError(f"ffprobe could not run: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-_MAX_STDERR:] or f"exit {completed.returncode}"
            raise ProbeError(f"ffprobe failed for {target.name}: {detail}")
        return parse_probe_json(completed.stdout)


def parse_probe_json(text: str) -> MediaInfo:
    """Parse ``ffprobe -show_format -show_streams -print_format json`` output."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProbeError("ffprobe output is not a JSON object")
    fmt = data.get("format", {})
    raw_streams = data.get("streams", [])
    if not isinstance(fmt, dict) or not isinstance(raw_streams, list):
        raise ProbeError("ffprobe output has an unexpected structure")
    streams = tuple(_parse_stream(s) for s in raw_streams if isinstance(s, dict))
    return MediaInfo(
        format_name=_str(fmt.get("format_name")),
        duration_s=_float(fmt.get("duration")),
        bit_rate=_int(fmt.get("bit_rate")),
        streams=streams,
        raw_json=json.dumps(data, separators=(",", ":"), sort_keys=True),
    )


def _parse_stream(raw: dict[str, Any]) -> StreamInfo:
    tags = _dict(raw.get("tags"))
    disposition = _dict(raw.get("disposition"))
    return StreamInfo(
        index=_int(raw.get("index")) or 0,
        codec_type=_str(raw.get("codec_type")) or "unknown",
        codec_name=_str(raw.get("codec_name")),
        profile=_str(raw.get("profile")),
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
        pix_fmt=_str(raw.get("pix_fmt")),
        avg_frame_rate=parse_rate(raw.get("avg_frame_rate")),
        r_frame_rate=parse_rate(raw.get("r_frame_rate")),
        rotation=_rotation(raw, tags),
        color_transfer=_str(raw.get("color_transfer")),
        color_primaries=_str(raw.get("color_primaries")),
        sample_rate=_int(raw.get("sample_rate")),
        channels=_int(raw.get("channels")),
        channel_layout=_str(raw.get("channel_layout")),
        language=_str(tags.get("language")),
        bit_rate=_int(raw.get("bit_rate")),
        duration_s=_float(raw.get("duration")),
        is_attached_pic=bool(disposition.get("attached_pic")),
    )


def parse_rate(value: object) -> float | None:
    """Parse an ffprobe rate such as ``30000/1001``; ``0/0`` gives None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate > 0 else None


def _rotation(raw: dict[str, Any], tags: dict[str, Any]) -> int:
    """Rotation in degrees clockwise, normalised to 0, 90, 180 or 270."""
    degrees: float | None = None
    side_data = raw.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and "rotation" in item:
                degrees = _float(item["rotation"])
                if degrees is not None:
                    degrees = -degrees  # display matrix is counter-clockwise
                break
    if degrees is None:
        degrees = _float(tags.get("rotate"))
    if degrees is None:
        return 0
    return round(degrees / 90) * 90 % 360


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
