# IftyEdits AI Studio — Project Instructions

## 1. Product mission

Build **IftyEdits AI Studio**, a reliable, local-first AI-assisted YouTube Shorts clipping and production studio.

The product should help its owner turn long-form videos into high-quality, engaging, vertical Shorts through a guided workflow:

1. Import and protect original video files.
2. Inspect and prepare media.
3. Transcribe speech with timestamps.
4. Detect interesting moments using explainable signals.
5. Generate and rank candidate clips.
6. Reframe clips into vertical 9:16 videos.
7. Create and edit captions.
8. Preview and refine clips.
9. Generate titles, descriptions, tags and upload metadata.
10. Run safety, policy and rights checks.
11. Export finished Shorts.
12. Optionally upload only after explicit human approval.
13. Track performance and learn from analytics.

This is intended to be a practical personal creator studio, not a demo, mock-up or collection of disconnected screens.

## 2. Product priorities

Prioritize, in this order:

- Reliability and protection of original media.
- Correctness of video/audio processing.
- Useful clips rather than arbitrary timestamps.
- Fast, understandable local workflows.
- Good vertical composition and readable captions.
- A polished, coherent user interface.
- Reproducibility and explainability.
- Low recurring cost and minimal external dependencies.
- Safe publishing workflows with human approval.

The first target is a high-quality local application for one creator on an Apple Silicon Mac with 32 GB RAM. Do not prematurely design for massive distributed infrastructure.

## 3. User and content workflow

The owner runs the IftyEdits YouTube Shorts channel. Content interests include podcast highlights, creator/streamer moments, gaming, challenges, entertainment and motivational content.

The application must not assume that a source video is automatically safe to reuse. Copyright permission, platform rules, privacy, harassment, defamation and music rights are separate concerns. Never promise that a clip is copyright-free or guaranteed fair use.

The application should help the user make original, meaningful edits and retain human control over final selection and publishing.

## 4. Existing technical baseline

The repository has an established Python foundation. Before changing anything, inspect the actual repository and current branch. Do not assume this document describes every current file.

Known baseline after Phase 1A (branch `feat/phase-1a-import`, PR #1):

- Python 3.12, managed with `uv` (never system Python); `uv.lock` committed.
- Hatchling packaging; the `iftyedits` command is defined in `pyproject.toml`.
- Ruff linting and formatting.
- Strict mypy over `src`, `tests`, `scripts` and `app`.
- pytest with strict markers (`ffmpeg`, `slow`).
- Existing layers in `src/iftyedits/`: `core`, `storage`, `adapters`, `services`, `pipeline`, `ui`. Streamlit pages live in `app/`.
- Existing configuration in `config.py` (pydantic-settings, `IFTYEDITS_` environment variables and `.env`).
- Existing error types in `core/errors.py`.
- Existing protected-original storage implementation in `storage/originals.py`.
- Existing SQLite schema with numbered migrations in `storage/migrations/` (currently `0001`, `0002`).
- Existing SQLite job queue and background worker in `pipeline/`.
- Existing environment checker in `scripts/check_env.py`.
- Existing CI (`.github/workflows/ci.yml`) has two jobs: `checks` on `macos-26` (Apple Silicon) runs locked dependency sync, Ruff, formatting, mypy and the non-FFmpeg tests; `integration` on `ubuntu-24.04` runs the FFmpeg and Streamlit server tests with `IFTYEDITS_REQUIRE_FFMPEG=1`.
- The baseline is 177 passing tests (164 unit/fast, 13 FFmpeg/slow), measured 2026-11-19. Re-run the tests and establish the actual current count before relying on this number.
- The installed Homebrew FFmpeg lacks `libass`/`freetype` (`subtitles`, `ass` and `drawtext` filters are missing). Caption burn-in must render overlays in Python and use the `overlay` filter, or wait for an approved FFmpeg change.

The implementation roadmap is in `docs/ROADMAP.md`; current status is in `docs/PROGRESS.md`. Update them when implementation changes the plan.

## 5. Architecture principles

Use a modular, testable architecture:

- `core`: domain models, protocols, validation and domain errors.
- `storage`: database, repositories, migrations and protected original media.
- `adapters`: FFmpeg, FFprobe, transcription engines and external integrations.
- `services`: use cases and orchestration.
- `pipeline`: processing stages and job execution.
- `ui` (`src/iftyedits/ui/`): display formatting shared by the Streamlit pages. No business logic, no I/O.
- `app`: Streamlit user interface (`app/Home.py`, `app/views/`). Thin: it validates input, enqueues jobs and reads persisted state.
- `api`: a FastAPI layer, added only when needed. It must reuse services and never duplicate their logic. It does not exist yet.
- `scripts`: developer and maintenance commands.
- `tests`: unit, integration and end-to-end tests.

Dependencies point inward: `core` imports nothing from other layers; `services` never imports `app`, `ui` or `api`. Adapters are the only code that runs external tools.

Keep business logic out of UI code. The UI should call services and read persisted state. Long-running media work must run through a job/worker system, not inside a Streamlit page request.

Prefer standard-library solutions when they are sufficient. Use SQLite initially; do not add a server database without a demonstrated need.

Use numbered SQL migrations and `PRAGMA user_version` unless repository evidence establishes a better existing migration approach.

Use explicit interfaces/protocols for replaceable engines. Avoid unnecessary abstractions and speculative frameworks.

### Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 (uv-managed, never system Python) |
| Package / environment manager | uv, local `.venv`, `uv.lock` committed |
| Dashboard | Streamlit, bound to `127.0.0.1` |
| Configuration | pydantic-settings (environment variables and `.env`) |
| Backend API | FastAPI, only when needed (not added yet) |
| Storage | SQLite (metadata), filesystem (media) |
| Video processing | FFmpeg / ffprobe via subprocess (Homebrew build; do not reinstall without approval) |
| Transcription (planned, Phase 1C) | faster-whisper (CPU int8) and mlx-whisper, to be benchmarked |
| Scene detection (planned, Phase 2A) | PySceneDetect or a justified alternative |
| Computer vision (planned, Phase 2A) | OpenCV |
| Lint / format | Ruff |
| Type checking | mypy (strict) |
| Tests | pytest |

Planned entries are not installed. Add a dependency only when a task needs it, after the checks in section 8.

External processes: use `subprocess.run([...])` with an argument list and explicit timeouts. Never use `shell=True`. Use `pathlib.Path` for paths.

## 6. Non-negotiable safety rules

- Never overwrite, modify, move, rename, delete or hard-link a user's original source media.
- Import originals into a protected vault and verify their SHA-256 hashes.
- All generated files must live in designated derived/export directories.
- Reject path traversal and unsafe symlink destinations.
- Do not follow untrusted paths outside approved storage roots.
- Never silently discard a source file after an error.
- A failed database write must not destroy an already-imported original.
- Detect and recover from interrupted imports without creating misleading database records.
- Do not automatically delete original media, even when storage is low.
- Never store credentials, API keys, OAuth tokens or secrets in Git.
- Never upload to YouTube or another service without explicit user approval for that upload.
- Never publish, schedule or make a video public automatically.
- Never bypass platform safety checks or claim that automated checks guarantee policy compliance.
- Never weaken, delete, skip or falsify a test merely to make CI green.
- Never use `--no-verify`, suppress errors, or hide failing tests to claim success.
- Never push, deploy, release or publish without explicit user approval.

If a destructive, irreversible, paid, external or security-sensitive action is necessary, stop and request human approval.

How the current code enforces these rules (`storage/originals.py`, `services/ingest.py` and their tests; do not bypass them):

- Imports are independent byte copies, hashed with SHA-256 in the same pass as the copy. The import is rejected if the source's size or modification time changes during the copy.
- Copies are written to `data/staging/`, made read-only (mode `0444`) and moved into `data/originals/<sha[:2]>/` under a lock. An existing vault file is never replaced.
- Generated output must live under `data/derived/` or `data/exports/` and pass `assert_safe_output_path()` before it is written.
- Automatic cleanup is limited to abandoned `*.partial` files in `data/staging/`. Nothing automated touches `data/originals/`.
- If the database write fails, the vault copy is kept and `iftyedits reconcile` records it.
- Streamlit binds to `127.0.0.1` by default. Any future local server must do the same.

## 7. Engineering standards

- Inspect before editing. Understand callers, tests and data flow.
- Make small, cohesive, reviewable changes.
- Add or update tests with every meaningful behavior change.
- Test success cases, failure cases, edge cases and security boundaries.
- Use type hints and satisfy strict mypy.
- Keep Ruff lint and format checks clean.
- Use clear domain-specific errors and actionable messages.
- Avoid broad exception handling that hides the cause of failure.
- Avoid global mutable state.
- Make processing deterministic where practical.
- Make jobs restartable and idempotent where practical.
- Record stage status, progress, errors and timestamps.
- Log useful diagnostic information without leaking secrets or private content.
- Do not add a dependency until its purpose, compatibility, maintenance and platform support have been checked.
- Pin and lock dependencies using the repository's established `uv` workflow.
- Check official documentation for APIs and version-sensitive behavior.
- Do not invent APIs, command-line flags, model capabilities or compatibility claims.

## 8. Research and dependency selection

Before introducing a major library, model or external service:

1. Identify the exact requirement.
2. Compare credible alternatives.
3. Check official documentation and supported platforms.
4. Verify Python 3.12 and Apple Silicon compatibility.
5. Check transitive dependencies and conflicts.
6. Consider model download size, RAM, disk and inference speed.
7. Consider licensing and redistribution constraints.
8. Prefer local/offline processing where practical.
9. Record the decision and trade-offs in `docs/DECISIONS.md`.

Do not add multiple competing implementations of the same capability without a demonstrated reason.

## 9. Performance and hardware constraints

Primary development and runtime target: Apple Silicon Mac, M1 Max, 32 GB RAM, macOS.

- Avoid loading entire large videos into application memory.
- Prefer file paths and streaming/chunked processing.
- Serialize memory-heavy tasks when appropriate.
- Provide progress and cancellation for long jobs.
- Benchmark transcription and video processing on representative files.
- Treat faster-whisper CPU and MLX-based inference as alternatives to benchmark, not assumptions.
- Keep optional model dependencies optional.
- Provide graceful behavior when FFmpeg, FFprobe, models or hardware acceleration are unavailable.
- Never claim hardware acceleration is active without detecting and verifying it.
- Encode with VideoToolbox (`h264_videotoolbox`, `hevc_videotoolbox`) when it is detected, verified and quality allows; otherwise fall back to a reliable software encoder such as `libx264`.
- Run CPU-heavy inference with int8 quantisation or MLX where benchmarks justify it.
- Analyse downscaled proxies, not full-resolution sources, where accuracy allows.

## 10. User interface standards

Build a polished, coherent creator studio, not a generic admin dashboard.

The interface should make the workflow understandable:

- Library
- Import
- Video details
- Processing/jobs
- Transcript
- Candidate clips
- Clip editor
- Captions
- Preview
- SEO/metadata
- Safety review
- Export
- Settings

Use clear status indicators, useful empty states, actionable errors, progress feedback and confirmation before risky actions.

Do not create decorative controls that do nothing. Every visible action must work or be clearly marked as unavailable.

Keep the interface responsive and accessible. Do not block the UI while processing media.

## 11. Testing and verification

After each meaningful implementation unit, run the relevant tests.

Before declaring a phase complete, run all applicable checks:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

`docs/TESTING.md` documents these commands, test markers, FFmpeg requirements and CI. Use the exact commands supported by the current repository; inspect `pyproject.toml` and CI if uncertain.

Add FFmpeg integration tests using generated test media where practical. Mark them `@pytest.mark.ffmpeg` (and slow tests `@pytest.mark.slow`). Tests use `tmp_path` and must never touch the real `data/` directory. Tests must not depend on private user videos.

Test source preservation and hash integrity. Test malformed media, missing audio, unusual frame rates, rotations, path traversal, symlinks, duplicate imports, disk errors and interrupted jobs as relevant.

If a tool or dependency is unavailable, report the limitation. Do not report an unrun test as passed.

## 12. Execution workflow

Follow `docs/ROADMAP.md` in dependency order.

For every task:

1. Inspect the current implementation and Git status.
2. Identify the smallest useful next task.
3. State the acceptance criteria.
4. Research unfamiliar or version-sensitive technical decisions.
5. Implement the task.
6. Add or update tests.
7. Run the relevant checks.
8. Review the diff for correctness, security and scope.
9. Update `docs/ROADMAP.md` if the plan changed, and record decisions in `docs/DECISIONS.md`.
10. Record remaining issues and the next task in `docs/PROGRESS.md`.

Do not rebuild working features unnecessarily. Reconcile the roadmap against the real repository before every phase.

If blocked by missing credentials, external approval, unavailable hardware, unavailable services or ambiguous product decisions, implement independent work that can safely proceed and record the blocker. Do not fabricate success.

## 13. Git rules

- Work on the approved feature branch.
- Check `git status` before making changes.
- Do not overwrite or discard unrelated user changes.
- Do not reset, clean, force-push or rewrite history.
- Do not commit or push unless the user explicitly authorizes it.
- Keep changes organized and report the exact modified files.
- Preserve the existing CI and branch history.
- Use short-lived branches (`feat/<topic>`, `fix/<topic>`, `chore/<topic>`, `docs/<topic>`) and Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Never commit `.env*` (except `.env.example`), secrets, `.venv/`, media, databases, model weights or anything under `data/` except `.gitkeep`.

## 14. Definition of done

A feature is done only when:

- Its intended behavior exists and is connected to the actual application.
- It has meaningful tests.
- Relevant tests pass.
- Types, lint and formatting pass.
- Errors and edge cases are handled.
- Documentation is updated.
- No placeholder or fake functionality is presented as complete.
- The diff has been reviewed.
- Remaining limitations are documented.

A phase is not complete because its UI exists or because code was generated. It is complete only when its acceptance criteria are verified.

## 15. Project documents

Maintain these documents:

- `docs/ROADMAP.md` — phases, dependencies and acceptance criteria.
- `docs/PROGRESS.md` — current status, completed tasks, test results and next task.
- `docs/DECISIONS.md` — important technical decisions and trade-offs.
- `docs/TESTING.md` — how to run tests and integration checks.
- `docs/USER_GUIDE.md` — how to use the application as currently implemented.
- `README.md` — overview, setup, CLI and configuration reference.

Keep these documents accurate. Update them as the implementation evolves. Document only implemented behaviour as available; mark planned features as planned.

### Precedence when instructions conflict

Decision D11 in `docs/DECISIONS.md` records: "`CLAUDE.md` holds rules for all phases; `docs/ROADMAP.md` holds phases and acceptance criteria; `docs/PROGRESS.md` tracks status. Rules in `CLAUDE.md` win on conflict."

`docs/DECISIONS.md` itself states: "Do not rewrite past entries; add a new one that supersedes them."

## 16. Ralph Loop operating rules

The Ralph Loop plugin (`/ralph-loop`) re-runs one prompt until a completion promise appears or an iteration limit is reached. Use it only under these rules:

1. **Bounded tasks only.** One task, one module or feature slice, independently testable. Never use it for architecture, migrations, security code or anything touching `data/originals/`.
2. **Always set limits:** `--max-iterations` (maximum 25) and `--completion-promise`.
3. **Objective exit criteria.** The prompt names the exact commands that prove completion, normally `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` and `uv run pytest <scope>`.
4. **Promise only when true.** Output the completion promise only when every check passes. Never weaken, skip, delete or falsify tests to reach it.
5. **Scope fence.** The prompt lists the files the loop may change. Changes outside them are not allowed.
6. **No destructive actions:** no `git reset`, `git clean`, `git stash`, branch switching, history rewriting or force-push; no deleting files outside the scope; no dependency installs unless the prompt allows them.
7. **No automatic commit or push.** The loop never commits, pushes, merges, stages or unstages unless the user explicitly approves that action.
8. **Stop after three repeated failures.** If the same failure repeats three times, cancel the loop and report it.
9. **Human review before committing.** A human reviews the diff before anything produced by a loop is committed.

Prompt template:

```text
/ralph-loop "Implement <task> in <files>. Requirements: <list>.
Done when: uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest tests/unit/<scope> all pass.
Only modify <files>. Do not touch data/. Do not commit, push or stage.
When all checks pass, output <promise>TASK_DONE</promise>." --max-iterations 10 --completion-promise "TASK_DONE"
```