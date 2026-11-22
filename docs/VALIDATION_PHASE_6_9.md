# Phase 6.9 validation (2026-11-22)

Three kinds of evidence are kept apart below: automated tests, live AI validation, and human
review. Only the first has been done for AI; the second could not be done.

## Provider status (live AI)

- No provider is configured: no `IFTYEDITS_LLM_*` variables, no `.env` entry.
- Ollama is not installed (`ollama` not on PATH, nothing listening on 127.0.0.1:11434, no
  `~/.ollama`). No model was downloaded and no system setting was changed.
- **Live metadata, regeneration and safety-review quality is therefore unvalidated.** Wording,
  relevance, fabrication, clickbait, SEO usefulness and review accuracy on a real model are unknown.

Setup needed to finish the live check:

```bash
brew install ollama && ollama serve        # then, in another terminal:
ollama pull <model>                        # choose one; a 7-8B instruct model suits 32 GB
export IFTYEDITS_LLM_MODEL=<model>         # default endpoint is already http://127.0.0.1:11434/v1
uv run streamlit run app/Home.py
```

Then generate metadata and run a review for real clips and read the output (human review).

## Automated tests (no real model)

- `tests/unit/test_phase69_validation.py` (27 tests): the real `urllib` client against a local
  OpenAI-format HTTP stub on a real socket.
  - Request body (model, system+user messages, `stream: false`) and the bearer header.
  - HTTP 401/403/404/429/500/503 map to the right domain errors; the API key never appears in an
    error.
  - Malformed, empty, null, non-string and empty-choice replies raise a format error.
  - A slow server times out; a refused connection is "unavailable"; non-http(s) URLs are refused.
  - Through the service: a fenced-JSON reply is parsed, hashtags and tags are de-duplicated, the
    transcript reaches the prompt and the key does not; a provider outage never becomes a passing
    review; readiness survives closing and reopening the database; missing required attribution
    blocks confirmation; a restricted source cannot be confirmed; the package never contains
    secrets or absolute paths and names the right export.
- Earlier suites (`test_publish_core.py`, `test_publishing_service.py`, `test_ui_publish.py`)
  cover field regeneration (a hand-edited title is never overwritten), AI/you markers, flag
  dismissal needing a note, history surviving re-review, stale review and metadata, rights
  history and inheritance, readiness blockers, confirmation and revocation, an export outside
  `exports/`, and package uniqueness. Controlled-transcript parsing of evidence and timestamps is
  tested with canned model replies, so it shows the parser behaves, not that a model is accurate.

## Bug found and fixed

The saved package contained the absolute path of the export file (home folder and user name). It
now holds only the file name plus the render id (`short.export_file`, `short.export_render_id`),
so the package can be matched to the export version without leaking local paths.

## Browser (isolated library, stub AI server on 127.0.0.1)

The AI endpoint was a local stub returning canned JSON, so this checks wiring, not model quality.

- Upload package → Safety review: "Run content review" went over HTTP and showed "Low detected
  risk"; Metadata → "Generate everything" filled suggested titles, description, hashtags and tags
  and kept the hand-typed title.
- After stopping and restarting Streamlit, the metadata row, the review row and the clip's
  settings were still in the database and the page reloaded without error.
- The full confirm flow was not clicked through in the browser; it is covered by the service and
  AppTest tests.
- 390px: not repeated. No UI code changed after the earlier audit, which found no horizontal
  overflow on all 8 pages and all 6 Upload package tabs.
- The static 404s: they come from loading a sub-page URL directly (for example `/clip_editor`).
  The Streamlit client first asks `/clip_editor/_stcore/health` and `/host-config` (404), then
  falls back to `/_stcore/health` (200). No asset is missing and no function is affected. The
  `ERR_CONNECTION_REFUSED` lines in the console were from the earlier server being stopped.

## Quality gates

`IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -q`: 1141 passed, 0 failed, 1 skipped. Ruff, format
check (160 files) and mypy are clean.

## Remaining limitations

No real-model validation; a model's accuracy on evidence, timestamps and risk level is untested;
human review of generated text is still needed; the review reads transcripts only and is not a
YouTube policy check.
