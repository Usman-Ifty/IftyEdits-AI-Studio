"""Verify the local toolchain for IftyEdits development. Read-only; changes nothing."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PYTHON = (3, 12)
MIN_FREE_GB = 20
FFMPEG_ENCODERS = ("h264_videotoolbox", "hevc_videotoolbox", "libx264")
FFMPEG_FILTERS = ("scdet", "loudnorm", "overlay")
OPTIONAL_FILTERS = ("subtitles", "ass", "drawtext")


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=30).stdout


def _has_word(output: str, name: str) -> bool:
    return any(name in line.split() for line in output.splitlines())


def main() -> int:
    """Print a report and return a non-zero exit code if a hard requirement fails."""
    failures: list[str] = []
    warnings: list[str] = []

    version = sys.version_info[:2]
    print(f"python      {sys.version.split()[0]}  ({sys.executable})")
    if version != REQUIRED_PYTHON:
        failures.append(f"Python {REQUIRED_PYTHON} required, got {version}")
    if sys.prefix == sys.base_prefix:
        failures.append("Not running inside a virtual environment (use `uv run`)")

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    print(f"ffmpeg      {ffmpeg or 'MISSING'}")
    print(f"ffprobe     {ffprobe or 'MISSING'}")
    if ffmpeg is None or ffprobe is None:
        failures.append("ffmpeg/ffprobe not on PATH")
    else:
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"])
        filters = _run([ffmpeg, "-hide_banner", "-filters"])
        for name in FFMPEG_ENCODERS:
            if not _has_word(encoders, name):
                failures.append(f"FFmpeg encoder missing: {name}")
        for name in FFMPEG_FILTERS:
            if not _has_word(filters, name):
                failures.append(f"FFmpeg filter missing: {name}")
        for name in OPTIONAL_FILTERS:
            if not _has_word(filters, name):
                warnings.append(f"FFmpeg filter missing (caption burn-in): {name}")

    try:
        import ctranslate2
        import faster_whisper
    except ImportError:
        print("whisper     MISSING")
        failures.append("faster-whisper is not installed (run `uv sync`)")
    else:
        print(
            f"whisper     faster-whisper {faster_whisper.__version__}, "
            f"ctranslate2 {ctranslate2.__version__} (CPU)"
        )

    free_gb = shutil.disk_usage(ROOT).free / 1024**3
    print(f"disk free   {free_gb:.0f} GiB")
    if free_gb < MIN_FREE_GB:
        failures.append(f"Less than {MIN_FREE_GB} GiB free")

    for warning in warnings:
        print(f"WARN  {warning}")
    for failure in failures:
        print(f"FAIL  {failure}")
    print("OK" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
