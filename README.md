# IftyEdits AI Studio

Local-first AI studio that turns long-form video into YouTube Shorts.
Built for Apple Silicon (M1 Max). Development rules live in [CLAUDE.md](CLAUDE.md). The phased product plan is in
[docs/ROADMAP.md](docs/ROADMAP.md), status in [docs/PROGRESS.md](docs/PROGRESS.md) and
decisions in [docs/DECISIONS.md](docs/DECISIONS.md). How to use the app:
[docs/USER_GUIDE.md](docs/USER_GUIDE.md). How to run checks and tests:
[docs/TESTING.md](docs/TESTING.md).

**Status:** Phases 1A (import), 1B (media preparation and jobs) and 1C (transcription) are
implemented on local branches and awaiting review; see [docs/PROGRESS.md](docs/PROGRESS.md).
Videos are imported into a protected vault, prepared in a background worker (16 kHz audio,
720p proxy, thumbnail) and transcribed locally with faster-whisper into searchable,
timestamped transcripts that export to SRT, WebVTT, text and JSON. Phase 2 (clip
intelligence, implemented on `feat/phase-2-clip-intelligence`, awaiting review) analyses a
transcript with loudness and scene changes, proposes sentence-aligned clips and ranks them
with an explainable algorithmic score on the **Clips** page. Nothing is rendered yet.

## Requirements

- macOS on Apple Silicon (Linux works for development and CI)
- [uv](https://docs.astral.sh/uv/) (manages Python 3.12; system Python is not used)
- FFmpeg and ffprobe on `PATH` (Homebrew)
- Disk space for speech models (74 MB to 2.9 GB each, downloaded only on request)

## Setup

```bash
uv sync
cp .env.example .env
uv run iftyedits check
uv run iftyedits models download small   # optional now; also available in the UI
```

## Running

Start the dashboard and a background worker together (binds to `127.0.0.1:8501`):

```bash
uv run iftyedits ui
```

Or run them separately, in two terminals:

```bash
uv run iftyedits worker
uv run streamlit run app/Home.py
```

`.streamlit/config.toml` binds Streamlit to `127.0.0.1` and disables usage statistics.

### CLI

| Command | What it does |
|---|---|
| `iftyedits import PATH... [-r]` | Import files or folders now, in this process |
| `iftyedits enqueue PATH... [-r] [--process]` | Queue files or folders for the worker; `--process` also prepares media |
| `iftyedits worker [--once]` | Run queued jobs (imports, preparation, transcription, downloads) |
| `iftyedits process VIDEO... \| --all [--stage S] [--force]` | Queue media preparation (audio, proxy, thumbnail) |
| `iftyedits models [list\|download NAME\|verify NAME]` | List, download or verify speech models |
| `iftyedits transcribe VIDEO... \| --all [--model M] [--language L] [--force]` | Queue transcription |
| `iftyedits transcript VIDEO [--format txt\|srt\|vtt\|json] [--save]` | Print or save the current transcript |
| `iftyedits analyze VIDEO... \| --all [--force]` | Queue clip analysis (suggested Shorts) |
| `iftyedits clips list\|show\|favorite\|reject\|clear VIDEO [REF]` | Review ranked clip suggestions |
| `iftyedits search QUERY [--video VIDEO] [--limit N]` | Search current transcripts |
| `iftyedits jobs [--limit N] [--active]` | List jobs |
| `iftyedits cancel JOB_ID...` | Cancel queued jobs or stop running ones |
| `iftyedits artifacts VIDEO [--verify]` | Show preparation stages and generated files |
| `iftyedits list [--limit N]` | List imported videos (default 100) |
| `iftyedits show VIDEO [--verify]` | Show one video; `--verify` re-hashes the vault copy |
| `iftyedits reconcile` | Record vault files that have no database row |
| `iftyedits check` | Verify ffprobe, ffmpeg, faster-whisper, models, database and workers |
| `iftyedits ui [--port N] [--address A] [--no-worker]` | Start Streamlit plus a worker; binds to `127.0.0.1:8501` by default |

`VIDEO` is a video ID or a SHA-256 prefix of at least 6 characters.
`-r` / `--recursive` includes subfolders. Every entry that is not imported (symbolic link,
hidden file, other extension, subfolder without `-r`) is listed with its reason.

## Short editor

**Clips → Edit as a Short** opens an editor to trim a suggestion, frame it for 9:16, edit
captions, adjust look and sound, render a preview and export an H.264/AAC MP4 (1080x1920,
VideoToolbox when available, libx264 otherwise) to `data/exports/shorts/`. Captions are drawn
with Pillow because the installed FFmpeg has no text filters. Planned, not built: face-aware
reframing, transitions, multi-clip timelines, a CLI export command. See D24 in
[docs/DECISIONS.md](docs/DECISIONS.md).

## How import works

1. The path is resolved and checked: a regular file with an allowed extension, outside `data/`.
2. ffprobe reads the source. Files without a video stream are rejected before any copy.
3. If stored content has the same size, the source is hashed. Identical content
   (any filename or extension) is recorded as another source of the existing video.
4. Otherwise the file is copied once into `data/staging/` while its SHA-256 is computed,
   checked for changes during the copy, made read-only and moved into
   `data/originals/<sha[:2]>/<sha><ext>`.
5. The vault copy is probed and the video, its streams and its source path are
   written to SQLite in one transaction.

The source file is only ever opened for reading. It is never modified, moved, deleted or linked.
If the database write fails, the vault copy is kept and `iftyedits reconcile` records it later.
Interrupted copies are removed from `data/staging/`; the worker requeues interrupted jobs.

The Streamlit pages only queue jobs. Copying, probing, FFmpeg work, model downloads and
transcription run in the worker process.

## How transcription works

1. The job checks that the video has an audio stream and that the model is downloaded
   (transcription never downloads anything).
2. It uses the 16 kHz mono WAV from media preparation, regenerating it from the original if
   it is missing or damaged, and checks the WAV header.
3. faster-whisper (CTranslate2, CPU, int8) transcribes it with voice-activity detection and
   word timestamps, reporting progress per segment; cancellation is checked between segments.
4. Timings are validated and normalised, then the transcript, its segments and words are
   stored in SQLite in one transaction, with an FTS5 search index. The same settings reuse the
   stored transcript; different settings add a version, and one version per video is current.

Details: decisions D15 to D18 in [docs/DECISIONS.md](docs/DECISIONS.md).

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                     # all tests (FFmpeg tests need ffmpeg installed)
uv run pytest -m "not ffmpeg and not slow"   # fast unit tests only
IFTYEDITS_RUN_MODEL_TESTS=1 uv run pytest -m model   # real speech model (needs `models download tiny`)
```

## Layout

```
src/iftyedits/
  config.py      settings (env vars and .env)
  cli.py         `iftyedits` command
  core/          domain models, protocols and errors (no I/O)
  storage/       SQLite (db.py, migrations/), video repository, originals vault
  adapters/      ffprobe, ffmpeg, faster-whisper, speech model store
  services/      ingest, media preparation, transcription, subtitle exports, clip analysis
  pipeline/      job queue and worker
  ui/            display formatting for Streamlit
app/             Streamlit dashboard (Home.py, views/)
tests/           unit/ and integration/
scripts/         developer utilities
data/            local data, git-ignored
  originals/     read-only vault
  staging/       in-progress copies
  derived/       generated audio, proxies, thumbnails
  exports/       saved transcript exports (finished Shorts in later phases)
  db/            SQLite database (including transcripts)
models/          speech models (faster-whisper/<name>/), git-ignored
```

## Configuration

See [.env.example](.env.example). Precedence: environment > `.env` > defaults.
Relative paths resolve against `IFTYEDITS_HOME`, or the current directory if unset.

| Variable | Default | Purpose |
|---|---|---|
| `IFTYEDITS_HOME` | current directory | Project root for `.env` and relative paths |
| `IFTYEDITS_DATA_DIR` | `./data` | Root of local data |
| `IFTYEDITS_MODELS_DIR` | `./models` | Model weights cache |
| `IFTYEDITS_LOG_LEVEL` | `INFO` | Log level |
| `IFTYEDITS_FFPROBE_PATH` | `ffprobe` | ffprobe executable |
| `IFTYEDITS_PROBE_TIMEOUT_S` | `60` | ffprobe timeout per file |
| `IFTYEDITS_FFMPEG_PATH` | `ffmpeg` | ffmpeg executable |
| `IFTYEDITS_FFMPEG_TIMEOUT_S` | `21600` | Limit for one FFmpeg command |
| `IFTYEDITS_PROXY_ENCODER` | `auto` | `auto`, `h264_videotoolbox` or `libx264` |
| `IFTYEDITS_MIN_FREE_BYTES` | `2147483648` | Free space that must remain after an import |
| `IFTYEDITS_VIDEO_EXTENSIONS` | `.mp4,.m4v,.mov,.mkv,.webm,.avi,.mts,.m2ts,.mxf` | Accepted extensions |
| `IFTYEDITS_TRANSCRIPTION_MODEL` | `small` | Default speech model |
| `IFTYEDITS_TRANSCRIPTION_LANGUAGE` | `auto` | `auto` or a language code such as `en` |
| `IFTYEDITS_TRANSCRIPTION_COMPUTE_TYPE` | `int8` | `int8`, `int8_float32` or `float32` |
| `IFTYEDITS_TRANSCRIPTION_CPU_THREADS` | `0` | CPU threads (0 = faster-whisper default) |
| `IFTYEDITS_TRANSCRIPTION_BEAM_SIZE` | `5` | Beam size (1 to 10) |
| `IFTYEDITS_TRANSCRIPTION_VAD` | `true` | Skip silence with voice-activity detection |
| `IFTYEDITS_TRANSCRIPTION_WORD_TIMESTAMPS` | `true` | Store word timings |
| `IFTYEDITS_TRANSCRIPTION_TIMEOUT_S` | `21600` | Limit for one transcription job |
| `IFTYEDITS_CLIP_MIN_S` / `_TARGET_S` / `_MAX_S` | `15` / `40` / `60` | Preferred clip length in seconds (min <= target <= max) |
| `IFTYEDITS_CLIP_MAX_CANDIDATES` | `20` | Suggestions kept per video |
| `IFTYEDITS_CLIP_MAX_OVERLAP` | `0.5` | Largest overlap allowed between two suggestions (0 to 1) |
| `IFTYEDITS_CLIP_SCENE_THRESHOLD` | `0.3` | FFmpeg scene-change sensitivity |
| `IFTYEDITS_CLIP_USE_AUDIO` / `_USE_SCENES` | `true` | Use loudness and scene changes as evidence |
