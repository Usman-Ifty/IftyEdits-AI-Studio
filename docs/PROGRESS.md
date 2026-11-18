# Progress

Tracks each phase of [ROADMAP.md](ROADMAP.md) in the roadmap's progress format.
Update after every completed milestone.

| Phase | Status |
|---|---|
| 1A Import foundation | IN PROGRESS (PR open, awaiting CI and review) |
| 1B Media preparation and jobs | NOT STARTED (job queue and worker already exist from 1A) |
| 1C Transcription | NOT STARTED |
| 2A Content signals | NOT STARTED |
| 2B Candidate generation | NOT STARTED |
| 2C Clip scoring | NOT STARTED |
| 3A Vertical reframing | NOT STARTED |
| 3B Captions | NOT STARTED |
| 3C Editor and preview | NOT STARTED |
| 5 Export and batch | NOT STARTED |
| 4 SEO metadata | NOT STARTED |
| 6 Safety review | NOT STARTED |
| 7 YouTube integration | NOT STARTED |
| 8 Analytics | NOT STARTED |
| 9 Release readiness | NOT STARTED |

---

## Phase 1A — Video Import Foundation

**Status:** IN PROGRESS. Implementation complete on branch `feat/phase-1a-import`,
[PR #1](https://github.com/Usman-Ifty/IftyEdits-AI-Studio/pull/1). Marked COMPLETE after CI
passes and the PR is merged.

### Completed tasks

- 1A.1 SQLite schema (`videos`, `video_sources`, `media_streams`, `jobs`, `workers`),
  numbered migrations (`0001_initial`, `0002_stream_time_base`) versioned by
  `PRAGMA user_version`, transactional repository, migration/upgrade/rollback tests.
- 1A.2 pydantic-settings with `.env`, `IFTYEDITS_HOME`, validated log level and extensions.
- 1A.3 ffprobe adapter: duration, dimensions, codecs, pixel format, average and nominal
  frame rate, time base, bitrate, audio, language, rotation/display matrix, VFR, HDR,
  cover art; domain errors for malformed output and timeouts.
- 1A.4 Ingest: path/type validation, probe before copy, single-pass copy + SHA-256,
  duplicate detection across names and extensions with source records, disk-space
  reserve and `ENOSPC` handling, interruption cleanup, DB-failure safety and `reconcile`.
- 1A.5 CLI: `check`, `import`, `enqueue`, `worker`, `list`, `show [--verify]`,
  `reconcile`, `ui`. Progress output; every skipped folder entry is printed with a reason
  and counted; paths with spaces and Unicode are tested.
- 1A.6 Streamlit: Overview, Import (file and folder paths, no uploader, skipped-item list,
  live job table with outcome), Library (table with source count, details with streams,
  integrity, sources and duplicate notice). Heavy work runs only in the worker.

### Acceptance criteria

| Criterion | Evidence |
|---|---|
| Import MP4 and MOV | `test_full_import_preserves_source`, `test_video_without_audio_is_accepted` |
| Reject unsupported or corrupt files | `test_non_video_inputs_are_rejected_before_copy` (audio-only, text, truncated) |
| Correct metadata | `test_ffprobe_reads_generated_clip`, `test_ffprobe_parse.py` fixtures |
| Persist in SQLite | `test_db.py`, `test_full_import_preserves_source` |
| Duplicates by content hash | `test_renamed_copy_with_other_extension_is_duplicate` |
| Source bytes and hash preserved | `test_full_import_preserves_source`, `test_source_is_unchanged_by_import` |
| Interrupted import and DB failure | `test_interrupted_import_leaves_no_partial_and_source_intact`, `test_database_failure_keeps_original_and_reconcile_recovers` |
| Media shown in UI | `test_library_page_lists_imported_video`, `test_library_marks_duplicate_imports` |
| Tests, lint, format, types | see results below |

### Files

See PR #1 for the full list. Main modules: `src/iftyedits/{config,cli}.py`,
`core/{models,protocols,errors}.py`, `adapters/ffprobe.py`, `storage/{db,videos,originals}.py`,
`storage/migrations/`, `services/ingest.py`, `pipeline/{jobs,worker}.py`, `ui/formatting.py`,
`app/Home.py`, `app/views/`.

### Dependencies

- `streamlit` (runtime): dashboard.
- `pydantic-settings` (runtime): environment and `.env` configuration.
- `watchdog` (dev): faster Streamlit reload during development.

### Commands and results (2026-11-18, local, M1 Max)

```text
uv lock --check                              Resolved 52 packages
uv run ruff check .                          no issues
uv run ruff format --check .                 43 files already formatted
uv run mypy                                  no issues (strict, src tests scripts app)
uv run pytest (original 45 test files)       45 passed
uv run pytest -m "not ffmpeg and not slow"   164 passed, 13 deselected
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -m "ffmpeg or slow"   13 passed
uv run pytest                                177 passed
```

### Known limitations

- Duplicates are byte-exact; a remuxed or re-encoded copy is stored again.
- `reconcile` cannot recover the original filename or source path.
- The source is probed, then the vault copy is probed again (copy and hash are one pass).
- One job runs at a time; no cancellation yet (planned in 1B).
- No thumbnails or in-app preview yet (planned in 1B with proxies).
- FFmpeg lacks `libass`/`freetype`; irrelevant until captions (3B).

### Remaining work

- CI green on PR #1 (`checks` on macos-26, `integration` on ubuntu-24.04).
- Review and merge.

### Next recommended task

Phase 1B: add `cancelled` jobs and cancellation, stage records with parameters and output
hashes, 16 kHz mono WAV extraction, 720p proxies and thumbnails.
