"""Benchmark faster-whisper models on this machine.

Usage (models must already be installed with ``iftyedits models download NAME``)::

    uv run python scripts/benchmark_transcription.py --models tiny base --threads 0 4 8
    uv run python scripts/benchmark_transcription.py --audio speech.wav --reference ref.txt

Without ``--audio`` a speech sample is synthesised with macOS ``say`` from a fixed
English script, converted to 16 kHz mono WAV with FFmpeg, and the script's text is used
as the reference for the word error rate (WER). Synthetic speech is far cleaner than a
real podcast, so WER here is a lower bound; use ``--audio`` with a real recording and a
hand-checked ``--reference`` for representative numbers.

Each configuration runs in a fresh subprocess so load time and peak memory are not
affected by earlier runs. Prints a Markdown table (and JSON with ``--json``).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iftyedits.adapters.faster_whisper import load_wav  # noqa: E402
from iftyedits.adapters.model_store import ModelStore  # noqa: E402
from iftyedits.config import Settings  # noqa: E402
from iftyedits.core.models import ModelState  # noqa: E402

SCRIPT = (
    "Welcome back to the show. Today we are talking about how creators turn long videos "
    "into short clips that people actually want to watch. The first thing to understand "
    "is that the hook matters more than anything else. If the first three seconds are "
    "boring, viewers scroll away. Second, captions help a lot, because many people watch "
    "without sound. Third, keep the story simple: one idea per clip. Our guest today has "
    "grown a gaming channel from zero to two hundred thousand subscribers in eighteen "
    "months. She says consistency beat everything, even expensive equipment. Let's get "
    "into it and talk about money, motivation and the challenges nobody tells you about."
)


def words(text: str) -> list[str]:
    """Lower-case words without punctuation, for WER."""
    return re.findall(r"[a-z0-9']+", text.lower())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over words divided by the reference length."""
    ref, hyp = words(reference), words(hypothesis)
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i]
        for j, h in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / max(1, len(ref))


def synthesise(folder: Path, repeats: int) -> Path:
    """Speech WAV from the fixed script (macOS ``say`` + FFmpeg)."""
    say, ffmpeg = shutil.which("say"), shutil.which("ffmpeg")
    if say is None or ffmpeg is None:
        raise SystemExit("Need macOS `say` and ffmpeg to synthesise speech; pass --audio instead")
    aiff = folder / "speech.aiff"
    subprocess.run([say, "-o", str(aiff), " ".join([SCRIPT] * repeats)], check=True, timeout=600)
    wav = folder / "speech.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        check=True, timeout=600,
    )  # fmt: skip
    return wav


def run_single(model_dir: str, audio: str, compute_type: str, threads: int, beam: int) -> None:
    """Child process: transcribe once and print one JSON line."""
    from faster_whisper import WhisperModel

    samples = load_wav(Path(audio))
    started = time.perf_counter()
    model = WhisperModel(
        model_dir, device="cpu", compute_type=compute_type, cpu_threads=threads,
        local_files_only=True,
    )  # fmt: skip
    loaded = time.perf_counter()
    segments, info = model.transcribe(
        samples, beam_size=beam, word_timestamps=True, vad_filter=True
    )
    text = " ".join(s.text for s in segments)
    finished = time.perf_counter()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak if platform.system() == "Darwin" else peak * 1024
    print(
        json.dumps(
            {
                "load_s": loaded - started,
                "transcribe_s": finished - loaded,
                "language": info.language,
                "language_probability": info.language_probability,
                "peak_rss_bytes": peak_bytes,
                "text": text,
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", nargs="+", default=["tiny"])
    parser.add_argument("--threads", nargs="+", type=int, default=[0])
    parser.add_argument("--compute-types", nargs="+", default=["int8"])
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--audio", type=Path, help="16 kHz mono WAV (default: synthesised)")
    parser.add_argument("--reference", type=Path, help="reference transcript for --audio")
    parser.add_argument("--repeats", type=int, default=2, help="script repetitions to synthesise")
    parser.add_argument("--json", action="store_true", help="also print raw JSON results")
    parser.add_argument("--single", nargs=5, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.single:
        model_dir, audio, compute_type, threads, beam = args.single
        run_single(model_dir, audio, compute_type, int(threads), int(beam))
        return 0

    store = ModelStore(Settings.from_env().models_dir)
    with tempfile.TemporaryDirectory() as tmp:
        if args.audio is not None:
            audio = args.audio
            reference = args.reference.read_text(encoding="utf-8") if args.reference else None
        else:
            audio = synthesise(Path(tmp), args.repeats)
            reference = " ".join([SCRIPT] * args.repeats)
        with wave.open(str(audio), "rb") as handle:
            duration = handle.getnframes() / handle.getframerate()

        results = []
        for name in args.models:
            status = store.status(name)
            if status.state is not ModelState.INSTALLED:
                print(f"skip {name}: not installed ({status.detail})", file=sys.stderr)
                continue
            for compute_type in args.compute_types:
                for threads in args.threads:
                    child = subprocess.run(
                        [sys.executable, __file__, "--single", str(status.path), str(audio),
                         compute_type, str(threads), str(args.beam_size)],
                        capture_output=True, text=True, check=False, timeout=7200,
                    )  # fmt: skip
                    if child.returncode != 0:
                        print(f"{name} failed: {child.stderr[-500:]}", file=sys.stderr)
                        continue
                    row = json.loads(child.stdout.strip().splitlines()[-1])
                    row.update(model=name, compute_type=compute_type, threads=threads)
                    if reference is not None:
                        row["wer"] = word_error_rate(reference, row["text"])
                    results.append(row)
                    print(f"done {name} {compute_type} threads={threads}", file=sys.stderr)

    machine = f"{platform.machine()} macOS {platform.mac_ver()[0]}"
    print(f"Audio: {duration:.1f} s, {machine}, beam {args.beam_size}")
    print()
    header = ["Model", "Compute", "Threads", "Load (s)", "Transcribe (s)", "Speed (x)", "Peak RAM"]
    print("| " + " | ".join([*header, "WER"]) + " |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        wer = f"{r['wer']:.1%}" if "wer" in r else "-"
        print(
            f"| {r['model']} | {r['compute_type']} | {r['threads'] or 'default'} | "
            f"{r['load_s']:.1f} | {r['transcribe_s']:.1f} | {duration / r['transcribe_s']:.1f} | "
            f"{r['peak_rss_bytes'] / 1024**3:.2f} GB | {wer} |"
        )
    if args.json:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
