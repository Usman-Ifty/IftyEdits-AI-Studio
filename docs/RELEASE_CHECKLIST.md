# Release checklist

Run every item before calling a version ready. Record results in PROGRESS.md.

## 1. Code gates

```bash
uv sync --locked
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

```bash
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest
```

```bash
IFTYEDITS_RUN_MODEL_TESTS=1 IFTYEDITS_TEST_MODEL=small uv run pytest -m model
```

## 2. Clean install

Clone into a new folder, run `uv sync --locked`, then `uv run iftyedits check`. A new checkout has
no speech model; `check` names the download command.

## 3. Pipeline on media you own

With `IFTYEDITS_DATA_DIR` set to a scratch folder: `import`, `process 1`, `produce 1`,
`worker --once`. Check the export is 1080x1920 H.264 with AAC audio, decodes fully, shows captions,
and the source file's SHA-256 is unchanged.

## 4. Failure cases

Missing FFmpeg or ffprobe, missing speech model, not enough disk space
(`IFTYEDITS_MIN_FREE_BYTES`) and a corrupt file must each give a clear message and import nothing
partial.

## 5. Dependencies

Check every package version in `uv.lock` against the OSV database (https://osv.dev) or run
`pip-audit`.

## 6. Open before a public release

- Real-media validation on permitted podcast, sports, slides, noisy and non-English recordings
  (Phase 9). Synthetic speech does not replace it.
- A real YouTube upload approved by the owner.
- A real Claude (or local model) run of metadata and safety review.
- CI on a pull request to `main`.

## Licensing notes

Informational, not legal advice. The studio does not bundle or redistribute FFmpeg, speech models
or fonts.

- The Homebrew FFmpeg in use is built with `--enable-gpl` and `--enable-version3` (including
  libx264 and libx265). Distributing it together with the studio would bring GPL obligations.
- Python packages: faster-whisper, CTranslate2, pydantic-settings, onnxruntime (MIT);
  Streamlit, tokenizers, huggingface-hub (Apache-2.0); NumPy, PyAV (BSD-style); Pillow (MIT-CMU);
  yt-dlp (Unlicense).
- Speech models are downloaded on request from Hugging Face; read each model page's licence before
  redistributing a model.
- The YuNet face model (OpenCV Zoo, MIT) is downloaded on request, pinned by revision and SHA-256.
- The repository has no `LICENSE` file; `pyproject.toml` marks it Proprietary.
