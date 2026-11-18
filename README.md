# IftyEdits AI Studio

Local-first AI studio that turns long-form video into YouTube Shorts.
Built for Apple Silicon (M1 Max). Development rules live in [CLAUDE.md](CLAUDE.md).

**Status:** Phase 1A, video import. Local files and folders are imported into a protected
vault, probed with ffprobe and listed in a Streamlit library.

## Requirements

- macOS on Apple Silicon (Linux works for development and CI)
- [uv](https://docs.astral.sh/uv/) (manages Python 3.12; system Python is not used)
- FFmpeg and ffprobe on `PATH` (Homebrew)

## Setup

```bash
uv sync
cp .env.example .env
uv run iftyedits check
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
| `iftyedits enqueue PATH... [-r]` | Queue files or folders for the background worker |
| `iftyedits worker [--once]` | Run queued jobs (imports) |
| `iftyedits list` | List imported videos |
| `iftyedits reconcile` | Record vault files that have no database row |
| `iftyedits check` | Verify ffprobe, data directory, database and workers |
| `iftyedits ui [--port N] [--no-worker]` | Start Streamlit on localhost plus a worker |

`-r` / `--recursive` includes subfolders. Symbolic links inside folders are skipped.

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

The Streamlit pages only queue jobs. Copying and probing run in the worker process.

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                     # all tests (FFmpeg tests need ffmpeg installed)
uv run pytest -m "not ffmpeg and not slow"   # fast unit tests only
```

## Layout

```
src/iftyedits/
  config.py      settings (env vars and .env)
  cli.py         `iftyedits` command
  core/          domain models, protocols and errors (no I/O)
  storage/       SQLite (db.py, migrations/), video repository, originals vault
  adapters/      ffprobe
  services/      ingest
  pipeline/      job queue and worker
  ui/            display formatting for Streamlit
app/             Streamlit dashboard (Home.py, views/)
tests/           unit/ and integration/
scripts/         developer utilities
data/            local data, git-ignored
  originals/     read-only vault
  staging/       in-progress copies
  derived/       generated audio, proxies, clips (later phases)
  exports/       finished Shorts (later phases)
  db/            SQLite database
models/          model weights, git-ignored
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
| `IFTYEDITS_MIN_FREE_BYTES` | `2147483648` | Free space that must remain after an import |
| `IFTYEDITS_VIDEO_EXTENSIONS` | `.mp4,.m4v,.mov,.mkv,.webm,.avi,.mts,.m2ts,.mxf` | Accepted extensions |
