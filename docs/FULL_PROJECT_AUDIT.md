# Full project audit (2026-11-22)

## Verified claims from Phase 6.10

| Claim | Result |
|---|---|
| 57 uncommitted paths | True at audit start (25 modified, 32 untracked). Now committed locally, see Git below. |
| No real Claude call | True. `ANTHROPIC_API_KEY` is not in the environment or `.env` (checked without printing). |
| Ollama not installed | True. `ollama` is not on PATH and `127.0.0.1:11434` is unreachable. |
| Gates | Re-run: Ruff, format and mypy clean; `IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -q` = 1201 passed, 0 failed, 1 skipped (real-model test, needs `IFTYEDITS_RUN_MODEL_TESTS=1`). The repo-hygiene secret scan only caught problems after the work was committed (it scans tracked files); fixed by splitting dummy key literals in tests and blanking numeric values in `.env.example`, without weakening the scan. |

## Git

- Real history is one linear line: `main` (2 commits) → `feat/phase-1a-import` → `feat/phase-1b-media` →
  `feat/phase-2-clip-intelligence` (Phases 2–4C). Phases 5 to 6.10 were never committed.
- No `phase/01…05` branches were invented. One new branch, `phase/06-publishing-ai-providers`, holds the
  previously uncommitted work as five commits: Phase 5 UI, Phase 6/6.9 publishing, Phase 6.10 AI code,
  AI tests, docs. Only the branch tip is verified green; the intermediate commits were not
  individually test-run (the publishing commit imports the router lazily).
- Remote `origin` = `github.com/Usman-Ifty/IftyEdits-AI-Studio`. `gh` is logged in as
  `muhammadusmanawan-dev`, which has **no write permission** there: `git push` returned HTTP 403.
  **Nothing was pushed.** No credentials were changed. `main` was never touched.
- `.env` is ignored and holds only a transcription model name. No secrets found in tracked files;
  the only `sk-…` strings are dummy values in tests.

## AI configuration findings

The "Claude is not connected" and "no Ollama model" messages were accurate: neither a Claude key nor a
local model name exists in this environment. Problems found and fixed:

- The Settings pills said "Ready"/"Named" when only a key or model name was present. They now say
  "Key set, not tested" / "Model named, not tested".
- Added **Test Claude (tiny request)**: one synthetic prompt, `max_tokens=16`, no retries, no project
  data. Reports auth, rate-limit, timeout or success with token counts.
- The local check now reports "nothing is listening", "reachable but model X is not installed
  (installed: …)", or "installed", and a separate **Test local model** does a real tiny generation.
- Four distinct states are now separate: key configured, connection reachable, live answer, unavailable.

## Startup

`uv run iftyedits ui` (Streamlit plus a worker, `127.0.0.1:8501`) or `uv run streamlit run app/Home.py`.
`python -m app` and `python run.py` do not exist. Launched during this audit on port 8612 with an
isolated data directory; it served HTTP 200.

## Backend checks

- All `iftyedits.*` modules import cleanly (no circular or broken imports).
- `core` imports nothing from other layers; `services`/`adapters` do not import `app` or `ui`.
- No `shell=True`; no key printing or logging found.
- Migrations 0001–0009 apply; schema version 9.

## Dependencies

Python 3.12 (`.python-version`; the system `python3` is 3.9 and is not used), uv 0.13.9. Outdated,
all patch-level: certifi, ctranslate2 4.9.0→4.9.1, filelock, librt, pydantic 2.14.3→2.14.4,
pydantic-core, ruff 0.17.5→0.17.6, watchdog. **Not upgraded**: nothing needed, and no feature gains
justify churn before closing Phase 6. Vulnerability scan: `pip-audit` hung without output and was
stopped, so **no vulnerability audit was completed**. CI files were not re-inspected beyond README.

## Not done / blocked

GitHub push (403). Live Claude and Ollama (no credentials / not installed). YouTube URL ingestion of
the reference link was not run (see GTA6_CLIPPING_EXPERIMENT.md).
