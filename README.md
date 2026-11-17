# IftyEdits AI Studio

Local-first AI studio that turns long-form video into YouTube Shorts.
Built for Apple Silicon (M1 Max). Development rules live in [CLAUDE.md](CLAUDE.md).

## Requirements

- macOS on Apple Silicon
- [uv](https://docs.astral.sh/uv/) (manages Python 3.12; system Python is not used)
- FFmpeg and ffprobe on `PATH` (Homebrew)

## Setup

```bash
uv sync
cp .env.example .env
uv run python scripts/check_env.py
```

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Layout

```
src/iftyedits/   application package
  core/          domain models and errors (no I/O)
  storage/       SQLite and file storage, original-media vault
  adapters/      FFmpeg, YouTube, LLM wrappers
  services/      one module per pipeline stage
  pipeline/      job runner
app/             Streamlit dashboard
tests/           unit/ and integration/
scripts/         developer utilities
data/            local data, git-ignored
  originals/     read-only imported copies
  derived/       generated audio, proxies, clips
  exports/       finished Shorts
  db/            SQLite database
models/          model weights, git-ignored
```

## Original media protection

Imported videos are copied into `data/originals/` as independent, read-only files
named by their SHA-256 hash. The source file is never modified, moved or linked.
All generated files go to `data/derived/` or `data/exports/`.

## Configuration

See [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `IFTYEDITS_DATA_DIR` | `./data` | Root of local data |
| `IFTYEDITS_MODELS_DIR` | `./models` | Model weights cache |
| `IFTYEDITS_LOG_LEVEL` | `INFO` | Log level |
