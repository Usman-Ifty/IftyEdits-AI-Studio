# Testing

How to set up a development environment and run the same checks as CI.
All commands run from the repository root.

## Requirements

| Tool | Version | Notes |
|---|---|---|
| macOS on Apple Silicon | macOS 26 used locally and in CI | Linux works for development (CI runs integration tests on Ubuntu 24.04) |
| [uv](https://docs.astral.sh/uv/) | any recent release | Installs and manages Python; system Python is not used |
| Python | 3.12 (`requires-python = ">=3.12,<3.13"`) | Pinned in `.python-version`; uv downloads it if missing |
| FFmpeg and ffprobe | on `PATH` (Homebrew: `brew install ffmpeg`) | Needed for the `ffmpeg`-marked tests and for importing real media |
| faster-whisper | installed by `uv sync` (runtime dependency) | Unit tests never load it for inference; only the opt-in `model` tests run it |
| A downloaded speech model | `uv run iftyedits models download tiny` (74 MB) | Only for the opt-in `model` tests and manual checks |

## Set up the environment

```bash
uv sync --locked      # create .venv from uv.lock, including the dev group; fails if uv.lock is out of date
cp .env.example .env  # optional; defaults work without it
```

`uv sync --locked` installs exactly what `uv.lock` records. Use plain `uv sync` only when you
intend to update the lock file after changing dependencies in `pyproject.toml`.

To check that the lock file matches `pyproject.toml` without changing anything:

```bash
uv lock --check
```

## Check the toolchain

```bash
uv run python scripts/check_env.py
```

Read-only. It reports the Python version, virtual environment, ffmpeg/ffprobe paths, the
faster-whisper and CTranslate2 versions, required FFmpeg encoders (`h264_videotoolbox`, `hevc_videotoolbox`, `libx264`) and filters (`scdet`,
`loudnorm`, `overlay`), and free disk space (at least 20 GiB). It prints `OK` or `FAILED` and
exits non-zero on failure. Missing `subtitles`, `ass` and `drawtext` filters are reported as
warnings only; the current Homebrew FFmpeg lacks them.

`uv run iftyedits check` also verifies ffprobe, ffmpeg, faster-whisper, the installed speech
models and the database, but it opens (and, on first
run, creates and migrates) the database under the configured data directory. Use it for the
application, not as a side-effect-free test.

## Lint and format (Ruff)

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # report formatting differences without changing files
```

To apply fixes (these modify files):

```bash
uv run ruff check . --fix
uv run ruff format .
```

Configuration is in `pyproject.toml` (`[tool.ruff]`): line length 100, Python 3.12 target,
rule sets `E W F I B UP SIM PTH S RUF`. The locked Ruff (0.17.5) also includes Markdown files
(`*.md`) in `ruff format`, so documentation must pass `ruff format --check .` too. Its
"N files already formatted" count is Python plus Markdown; `ruff check` lints only the Python
files. Current counts are recorded in [PROGRESS.md](PROGRESS.md).

## Type checking (mypy, strict)

```bash
uv run mypy
```

`mypy` runs in strict mode over `src`, `tests`, `scripts` and `app`, with the pydantic plugin
(`[tool.mypy]` in `pyproject.toml`). No file arguments are needed.

## Tests (pytest)

```bash
uv run pytest                                  # everything (counts in PROGRESS.md)
uv run pytest -m "not ffmpeg and not slow"     # fast tests only, as the CI `checks` job runs
uv run pytest -m "ffmpeg or slow"              # FFmpeg and Streamlit server tests
uv run pytest tests/unit/test_ingest.py        # one file
uv run pytest -k duplicate                     # tests whose name matches
```

Layout:

- `tests/unit/`: fast tests. They use a fake prober (`tests/conftest.py`) instead of ffprobe.
- `tests/integration/`: real media generated with FFmpeg at test time
  (`tests/integration/conftest.py`), and a test that starts the Streamlit server.
- `tests/transcription_fakes.py`: transcription test doubles: a WAV writer, a scripted speech
  engine (`FakeTranscriber`), an in-memory model store and a fake audio-preparation step. No
  test downloads a model or runs inference unless the opt-in `model` marker is enabled.

- `tests/clip_fakes.py`, `tests/clip_db_fakes.py`: a scripted podcast transcript, generated
  loudness WAVs, a ready-made analysis and database helpers for clip tests. Clip tests need no
  model or network; real FFmpeg scene detection is covered in
  `tests/integration/test_clip_signals_ffmpeg.py`.

Markers (declared in `pyproject.toml`; `--strict-markers` rejects unknown ones):

- `ffmpeg`: needs the ffmpeg/ffprobe binaries.
- `slow`: slow test.
- `model`: runs a real speech model. Skipped unless `IFTYEDITS_RUN_MODEL_TESTS=1` and the model
  is installed.

Without ffmpeg/ffprobe, `ffmpeg` tests are skipped with the reason `ffmpeg/ffprobe not installed`.
Set `IFTYEDITS_REQUIRE_FFMPEG=1` to turn that skip into a failure, as CI does:

```bash
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -m "ffmpeg or slow"
```

### Opt-in real-model test

`tests/integration/test_transcription_ffmpeg.py::test_real_model_transcribes_speech` synthesises
English speech with macOS `say`, imports it, prepares the audio with FFmpeg and transcribes it
with a real faster-whisper model. It checks the detected language, key words, that every segment
and word time lies inside the audio, and that search finds a word.

```bash
uv run iftyedits models download tiny          # once, 74 MB, into models/faster-whisper/
IFTYEDITS_RUN_MODEL_TESTS=1 uv run pytest -m model -ra
```

`IFTYEDITS_TEST_MODEL` picks another installed model (default `tiny`) and
`IFTYEDITS_TEST_MODELS_DIR` another models folder (default `models/` in the repository).
CI does not run it (no model download in CI).

### Transcription benchmark

```bash
uv run python scripts/benchmark_transcription.py --models tiny base small --threads 0 4 8
```

Runs each installed model in a fresh process on synthesised speech (or `--audio file.wav
--reference text.txt`) and prints load time, transcription time, speed relative to real time,
peak memory and word error rate as a Markdown table. Results are recorded in
[DECISIONS.md](DECISIONS.md) (D16).

Test data rules:

- Tests use `tmp_path` and never touch the real `data/` directory.
- Media is generated at test time and kept tiny. Tests never use private user videos.

pytest options come from `[tool.pytest.ini_options]`: `-ra --strict-markers --strict-config`,
and unhandled thread or unraisable exceptions are errors.

## Run every check

Run these before declaring work complete:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Verify transcription manually

With a worker running (`uv run iftyedits ui`), a downloaded model and an imported video that has
speech:

1. Transcripts page: pick the video, press **Transcribe**, watch the progress, then check the
   segment table and word timings against the audio.
2. Download the SRT and VTT files and open them next to the video in a player (QuickTime, IINA
   or VLC), or check them with FFmpeg:
   `ffprobe -v error -show_entries stream=codec_name -of csv=p=0 file.srt` prints `subrip`
   (`webvtt` for a .vtt file).
3. Search for a word you heard.
4. `uv run iftyedits show VIDEO --verify` prints `integrity   OK` (the vault copy is unchanged),
   and `shasum -a 256` of your source file equals the SHA-256 it prints.

## Verify clip analysis manually

With an imported video that has a transcript:

1. `uv run iftyedits analyze VIDEO`, then `uv run iftyedits worker --once`.
2. `uv run iftyedits clips list VIDEO` prints ranked suggestions; `clips show VIDEO 1` prints
   the breakdown.
3. In the dashboard open **Clips**, sort and filter, inspect a clip, press **Favorite**, then
   **Update suggestions** and check the scores change.
4. To try it without touching real data, set `IFTYEDITS_DATA_DIR`, `IFTYEDITS_HOME` and
   `IFTYEDITS_MODELS_DIR` to a scratch folder first.

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and on every pull request:

| Job | Runner | Steps |
|---|---|---|
| `checks` | `macos-26` (Apple Silicon) | `uv sync --locked`, `ruff check .`, `ruff format --check .`, `mypy`, `pytest -m "not ffmpeg and not slow"` |
| `integration` | `ubuntu-24.04` | installs FFmpeg with apt, `uv sync --locked`, `pytest -m "ffmpeg or slow" -ra` with `IFTYEDITS_REQUIRE_FFMPEG=1` |

Actions are pinned by commit SHA. See decision D10 in [DECISIONS.md](DECISIONS.md).

## Safe commands

`uv run` first syncs `.venv/` with `uv.lock` (it does not change `uv.lock` unless dependencies in
`pyproject.toml` changed). Add `--no-sync` to run against the existing environment unchanged,
for example `uv run --no-sync pytest`.

These commands do not modify tracked files, dependencies or application data:

- `uv lock --check`
- `uv run ruff check .`, `uv run ruff format --check .`
- `uv run mypy`
- `uv run pytest` (writes only to temporary directories and the git-ignored `.pytest_cache/`)
- `uv run python scripts/check_env.py`
- `uv run iftyedits --help`
- `uv run python scripts/benchmark_transcription.py` (reads installed models; writes only to a
  temporary folder)

These change files or state; run them deliberately:

- `uv sync` (may rewrite `uv.lock`), `uv sync --locked` (rewrites `.venv/`)
- `uv run ruff check . --fix`, `uv run ruff format .` (edit source files)
- any `iftyedits` command except `--help` (opens or creates the database under `data/`;
  `import`, `enqueue`, `worker` and `reconcile` add data; `models download` writes model files
  under `models/`)
