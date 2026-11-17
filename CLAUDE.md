# CLAUDE.md — IftyEdits AI Studio

Permanent development instructions for every human and AI contributor.
Read this file fully before changing anything in the repository.

---

## 1. Project vision

IftyEdits AI Studio turns long-form videos into publish-ready YouTube Shorts,
running **locally on an Apple Silicon Mac** with minimal recurring cost.

Pipeline (each stage is an independent module):

1. Video Import
2. AI Video Analysis
3. Transcription
4. Viral Moment Detection
5. Clip Scoring
6. Automatic Shorts Editing
7. Dynamic Captions
8. SEO Generation
9. Copyright and Safety Review
10. YouTube Publishing
11. Analytics

Guiding goals:

- **Local first.** Process on the machine. Cloud APIs are optional add-ons, never requirements.
- **Low recurring cost.** Prefer free, open-source tools. Any paid API must be optional and swappable.
- **No vendor lock-in.** OpusClip, or any similar SaaS, must never be a required dependency.
- **Originals are sacred.** Imported source media is never modified, moved or overwritten.

---

## 2. Architecture principles

- **Modular monolith.** One Python package, `src/iftyedits/`, split into layers:
  - `core/` — domain models, enums, errors. No I/O, no framework imports.
  - `storage/` — SQLite access, file layout, original-media vault.
  - `adapters/` — the only code that talks to external tools (FFmpeg, YouTube API, LLM APIs).
  - `services/` — one subpackage per pipeline stage. Services depend on `core`, `storage` and adapter *interfaces*.
  - `pipeline/` — job runner. Stages are idempotent and resumable.
  - `api/` — FastAPI layer, added only when needed. Must reuse services; never duplicates logic.
- **UI is thin.** `app/` (Streamlit) calls services. It holds no business logic.
- **Dependencies point inward.** `core` imports nothing from other layers. `services` never imports `app` or `api`.
- **Swappable backends behind interfaces.** Examples: `Transcriber` (faster-whisper, mlx-whisper), `LLMProvider` (local, API). Choose the backend in config.
- **Explicit data flow.** Stages read inputs and write outputs to known locations under `data/derived/`, and record results in SQLite.
- **Apple Silicon optimisation.**
  - Encode with VideoToolbox (`h264_videotoolbox`, `hevc_videotoolbox`) when quality allows.
  - Run CPU-heavy work with int8 quantisation or MLX; use process pools for parallel CPU stages.
  - Analyse downscaled proxies, not full-resolution sources.
- **Configuration** comes from environment variables / `.env`, loaded in `src/iftyedits/config.py`. No hard-coded paths outside config.

---

## 3. Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 (uv-managed, never system Python) |
| Package / env manager | uv, local `.venv`, `uv.lock` committed |
| Dashboard | Streamlit (Phase 1) |
| Backend API | FastAPI (when needed) |
| Storage | SQLite (metadata), filesystem (media) |
| Video processing | FFmpeg / ffprobe via subprocess (Homebrew build, do not reinstall) |
| Transcription | faster-whisper (CPU int8); mlx-whisper as optional Metal backend |
| Scene detection | PySceneDetect |
| Computer vision | OpenCV |
| Lint / format | Ruff |
| Type checking | mypy (strict) |
| Tests | pytest |

Add a dependency only when a task needs it. Justify each new dependency in the commit message.
Pin AI model versions in config; never download model weights into the repository.

Known environment limitation: the installed FFmpeg lacks `libass`/`freetype`
(`subtitles`, `ass`, `drawtext` filters are missing). Caption burn-in must either
render overlays in Python and use the `overlay` filter, or wait for an approved FFmpeg change.

---

## 4. Coding standards

- Python 3.12 features are allowed. Use `from __future__ import annotations` in modules.
- Full type hints everywhere. `mypy --strict` must pass for `src/`.
- Ruff lint and format must pass. Line length 100.
- Use `pathlib.Path`, never string paths.
- Use `subprocess.run([...], check=True)` with an argument list. **Never** `shell=True`.
- Small, pure functions where possible. Side effects live in adapters and storage.
- Raise domain errors from `iftyedits.core.errors`; do not swallow exceptions.
- Use `logging`, never `print`, in library code. Never log secrets or full user file contents.
- Docstrings for every public module, class and function: what, not how.
- No dead code, no commented-out code, no speculative abstractions.
- Names: `snake_case` functions and modules, `PascalCase` classes, `UPPER_CASE` constants.

---

## 5. Testing requirements

- Every feature ships with tests. Every bug fix ships with a regression test.
- Layout: `tests/unit/` (fast, no external tools), `tests/integration/` (FFmpeg, models, DB).
- Mark tests that need FFmpeg with `@pytest.mark.ffmpeg`, slow ones with `@pytest.mark.slow`.
- Tests use `tmp_path`. Tests must **never** touch the real `data/` directory.
- Media fixtures: tiny files only (< 1 MB), generated at test time where possible.
- Required checks before any commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

---

## 6. Git workflow

- Default branch: `main`. Keep it green.
- Work on short-lived branches: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`, `docs/<topic>`.
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- One logical change per commit. Run all checks before committing.
- Review `git status` and `git diff --staged` before every commit.
- Never force-push `main`. Never rewrite published history.
- Never commit: `.env*` (except `.env.example`), API keys, tokens, OAuth client secrets,
  `.venv/`, video/audio files, database files, model weights, generated media, anything under `data/` except `.gitkeep`.

---

## 7. Security requirements

- Secrets live only in `.env` (git-ignored) or the macOS Keychain. `.env.example` holds placeholder names only.
- Never print, log, commit or paste secrets into prompts, issues or PRs.
- Never put secrets or personal data in URLs or query strings.
- Validate and resolve every user-supplied path. Reject path traversal and writes outside `data/`.
- External processes: argument lists only, no shell interpolation, explicit timeouts for long jobs.
- Bind local servers (Streamlit, FastAPI) to `127.0.0.1` by default.
- OAuth tokens (YouTube) are stored outside the repository with `0600` permissions.
- New third-party dependencies must be well-known and actively maintained.

---

## 8. Data protection requirements

These rules are enforced by `iftyedits.storage.originals` and its tests. Do not bypass them.

1. **Imports are independent copies.** Never move, hard-link or symlink a user's source file.
2. **Every original gets a SHA-256 hash** at import. The copy is verified against the source hash.
3. **Originals are read-only.** Vault copies are set to mode `0444` and stored under `data/originals/`.
4. **Never overwrite.** Vault files are created with exclusive-create semantics; an existing file is never replaced.
5. **Generated files live separately** under `data/derived/` or `data/exports/`. Use `assert_safe_output_path()` before writing any output.
6. **The source file is opened read-only** and must be byte-identical after import.
7. **Deletion of originals** happens only through an explicit, user-confirmed action. No automated cleanup may touch `data/originals/`.
8. User media, transcripts and analytics stay on this machine unless the user explicitly publishes them.

---

## 9. AI development team

| Model | Role |
|---|---|
| Opus 5.5 | Architecture, complex engineering, code reviews |
| Sonnet 5.5 | Regular implementation and testing |
| Fable 5.2 | Unusually difficult reasoning problems |
| Haiku 5.5 | Lightweight tasks (renames, docs, small edits) |

Architectural decisions and reviews of data-protection or security code go to Opus 5.5.

---

## 10. Ralph Loop operating rules

The Ralph Loop plugin (Anthropic directory, `ralph-loop`) re-runs one prompt until a
completion promise appears or an iteration limit is hit. Use it only under these rules:

1. **Bounded tasks only.** One task, one module or feature slice, independently testable.
   Never use it for architecture, migrations, security code or anything touching `data/originals/`.
2. **Always set limits:** `--max-iterations` (default 10, maximum 25) and `--completion-promise`.
3. **Objective exit criteria.** The prompt must name the exact commands that prove completion,
   normally `uv run ruff check .`, `uv run mypy` and `uv run pytest <scope>`.
4. **Promise only when true.** Output the completion promise only when every check passes. Never weaken, skip or delete tests to reach it.
5. **Scope fence.** The prompt lists the files the loop may change. Changes outside them are not allowed.
6. **No destructive actions** inside a loop: no `git push`, no `git reset --hard`, no deleting files outside the scope, no dependency installs unless the prompt allows them.
7. **Work on a branch.** Commit at the end only, after a human or Opus review of the diff.
8. **Stop early** with `/cancel-ralph` if the loop repeats the same failure three times.

Prompt template:

```text
/ralph-loop "Implement <task> in <files>. Requirements: <list>.
Done when: uv run ruff check . && uv run mypy && uv run pytest tests/unit/<scope> all pass.
Do not modify files outside <files>. Do not touch data/originals.
When all checks pass, output <promise>TASK_DONE</promise>." --max-iterations 10 --completion-promise "TASK_DONE"
```

---

## 11. Definition of done (every feature)

A feature is done only when **all** of the following hold:

- [ ] Code is in the correct layer and respects the dependency direction.
- [ ] Public functions have type hints and docstrings.
- [ ] Unit tests cover the normal path, edge cases and failure modes.
- [ ] Integration test exists if the feature calls FFmpeg, a model or the database.
- [ ] `ruff check`, `ruff format --check`, `mypy` and `pytest` all pass.
- [ ] Original-media protection tests still pass; no code writes to `data/originals/` outside the vault.
- [ ] No secrets, media, databases or model weights are staged.
- [ ] New config keys are documented in `.env.example` and the README.
- [ ] New dependencies are justified in the commit message.
- [ ] The diff has been reviewed before commit.

---

## 12. Common commands

```bash
uv sync                       # create/refresh .venv from uv.lock
uv run pytest                 # run tests
uv run ruff check . --fix     # lint and autofix
uv run ruff format .          # format
uv run mypy                   # type check
uv run python scripts/check_env.py   # verify local toolchain
```
