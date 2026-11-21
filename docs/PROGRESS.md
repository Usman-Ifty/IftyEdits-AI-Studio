# Progress

Tracks each phase of [ROADMAP.md](ROADMAP.md) in the roadmap's progress format.
Update after every completed milestone.

| Phase | Status |
|---|---|
| 1A Import foundation | IN PROGRESS (implementation complete, CI green; awaiting review and merge of PR #1) |
| 1B Media preparation and jobs | IN PROGRESS (implementation complete and verified locally; committed locally on branch `feat/phase-1b-media`; awaiting CI and merge) |
| 1C Transcription | IN PROGRESS (implementation complete, verified locally and by the owner on a real video; committed locally with 1B; benchmark covers tiny and base only; awaiting CI and merge) |
| 2A Content signals | COMPLETE locally (branch `feat/phase-2-clip-intelligence`); face detection deferred |
| 2B Candidate generation | COMPLETE locally; LLM assistance, preview and boundary editing deferred |
| 2C Clip scoring | COMPLETE locally |
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

**Status:** IN PROGRESS. Implementation is complete on branch `feat/phase-1a-import`,
[PR #1](https://github.com/Usman-Ifty/IftyEdits-AI-Studio/pull/1). CI passed on the PR head
`9d4991d` (both jobs green, 2026-11-18). The phase is marked COMPLETE after the PR is reviewed
and merged.

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
| Tests, lint, format, types | see results below; CI green on PR #1 |

### Files

See PR #1 for the full list. Main modules: `src/iftyedits/{config,cli}.py`,
`core/{models,protocols,errors}.py`, `adapters/ffprobe.py`, `storage/{db,videos,originals}.py`,
`storage/migrations/`, `services/ingest.py`, `pipeline/{jobs,worker}.py`, `ui/formatting.py`,
`app/Home.py`, `app/views/`.

Documentation cleanup (2026-11-19 and 2026-11-20, not committed): `CLAUDE.md`, `README.md`,
`docs/PROGRESS.md`, new `docs/TESTING.md` and `docs/USER_GUIDE.md`.

### Dependencies

- `streamlit` (runtime): dashboard.
- `pydantic-settings` (runtime): environment and `.env` configuration.
- `watchdog` (dev): faster Streamlit reload during development.

### Commands and results

Local, 2026-11-18, M1 Max:

```text
uv lock --check                              Resolved 52 packages
uv run ruff check .                          no issues
uv run ruff format --check .                 43 files already formatted
uv run mypy                                  no issues (strict, src tests scripts app)
uv run pytest (the 45 pre-1A tests)          45 passed
uv run pytest -m "not ffmpeg and not slow"   164 passed, 13 deselected
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -m "ffmpeg or slow"   13 passed
uv run pytest                                177 passed
```

Local re-check, 2026-11-19, M1 Max (run without syncing the environment):

```text
uv lock --check                              Resolved 52 packages
uv run ruff check .                          All checks passed
uv run ruff format --check .                 45 files already formatted
uv run mypy                                  Success: no issues found in 40 source files
uv run pytest                                177 passed (ffmpeg/ffprobe present)
```

Ruff 0.17.5 includes Markdown files in `ruff format`, so its file count is Python plus
Markdown. On 2026-11-19 that was 45 = 40 Python + 5 Markdown. After the documentation cleanup
added `docs/TESTING.md` and `docs/USER_GUIDE.md`, it is 47 = 40 Python + 7 Markdown
(2026-11-20). `ruff check` and mypy still cover the same 40 Python files. The 2026-11-18 count
of 43 was not broken down at the time.

CI on PR #1 (head `9d4991d`, run 44253341921, 2026-11-18):

```text
checks       macos-26       success
integration  ubuntu-24.04   success
```

### Known limitations

- Duplicates are byte-exact; a remuxed or re-encoded copy is stored again.
- `reconcile` cannot recover the original filename or source path.
- The source is probed, then the vault copy is probed again (copy and hash are one pass).
- One job runs at a time; no cancellation yet (planned in 1B).
- No thumbnails or in-app preview yet (planned in 1B with proxies).
- FFmpeg lacks `libass`/`freetype`; irrelevant until captions (3B).

### Remaining work

- Owner review of PR #1. No reviews or comments yet.
- Decide what joins PR #1 before merge: the local commit `0089f00` (whitespace-only change to
  `docs/ROADMAP.md`, not pushed) and the Phase 1A documentation cleanup (rewritten `CLAUDE.md`,
  new `docs/TESTING.md` and `docs/USER_GUIDE.md`, this file). Not committed yet.
- If anything is pushed, CI must pass again on the new head.
- Merge PR #1 into `main` (owner approval required), then mark Phase 1A COMPLETE here.

### Next recommended task

Phase 1B, in dependency order:

1. Job schema migration `0003`: `cancelled` status, cancellation requests, and job updates
   restricted to the owning worker.
2. FFmpeg adapter and protocol.
3. Stage records with parameters, version and output hashes.
4. 16 kHz mono WAV extraction.
5. 720p proxies and thumbnails.
6. UI progress, cancellation and thumbnails.

---

## Phase 1B — Media Preparation and Job System

**Status:** IN PROGRESS. Implementation is complete and verified locally on branch
`feat/phase-1b-media` (created from `feat/phase-1a-import` at `0089f00`; nothing committed
yet). It is marked COMPLETE after review, commit, a green CI run and merge. Phase 1B depends
on Phase 1A (PR #1), which is not merged yet.

### Completed tasks

- 1B.1 Job lifecycle: migration `0003_processing` rebuilds `jobs` with a `cancelled` status
  and `cancel_requested_at` (existing rows and ids copied unchanged), and adds
  `processing_stages` and `artifacts`. State changes only apply to running jobs owned by the
  calling worker; stale workers cannot overwrite results; queued jobs cancel immediately and
  running jobs cancel cooperatively; stale recovery and shutdown honour pending
  cancellations. Decision D12.
- 1B.1 Stage and artifact records: latest state per (video, stage) with parameters and
  their hash, progress, timestamps, errors and job id; one current artifact per (video,
  kind) with relative path, size, SHA-256, parameter hash and metadata. Ownership is checked
  in the same transaction as each write. Decision D13.
- 1B.2 Worker: dispatches `import_file` and `process_video` jobs; a heartbeat thread reports
  progress, turns cancellation requests into a cancel event and detects lost ownership. An
  import queued with `process` queues media preparation when it finishes. On start the
  worker marks interrupted stages failed and removes abandoned `*.partial` outputs older
  than one hour. Imports can now be cancelled too (the partial vault copy is removed).
- FFmpeg adapter (`adapters/ffmpeg.py`): detects version, encoders and filters; verifies
  VideoToolbox with a test encode before using it; runs commands as argument lists with
  `-progress pipe:1`, timeouts and cancellation (SIGTERM, then SIGKILL after 5 s; reports
  only after exit); error output is truncated and has full paths replaced by file names.
- 1B.3 Audio: first audio stream to 16 kHz mono PCM WAV; videos without audio are recorded
  as `skipped`; output codec, rate, channels and duration are validated.
- 1B.4 Proxy: H.264 MP4, shorter side at most 720 px, never upscaled, even dimensions,
  rotation applied, constant frame rate (source average, capped at 30 fps), AAC stereo when
  the source has audio. VideoToolbox when verified, else libx264; a VideoToolbox failure
  during an encode is retried with libx264. Size, codec, audio presence and duration are
  validated. Decision D14.
- 1B.4 Thumbnail: one 640x360 JPEG per video from the proxy (or the original), chosen by
  FFmpeg's `thumbnail` filter starting 10% into the video, with a retry from the start for
  short clips.
- Reuse: a stage with a valid artifact (same parameter hash, size and SHA-256) is reused;
  damaged or missing outputs are regenerated; `force` regenerates everything. Outputs are
  written to `data/derived/<sha[:2]>/<sha>/` via `assert_safe_output_path`, through a
  hidden `*.partial` file and an atomic rename.
- UI: Library table shows Audio / Proxy / Thumbnail status per video; the details panel
  (refreshed every 2 s) shows the thumbnail, stage table with progress and errors, generated
  files, **Prepare media**, **Regenerate all** and **Cancel processing**. New Jobs page:
  active jobs with progress bars and **Cancel**, history and error list. Import page:
  "Prepare media after import" (on by default). Overview: "Media prepared" count.
- CLI: `enqueue --process`, `process VIDEO... | --all [--stage S] [--force]`,
  `jobs [--limit N] [--active]`, `cancel JOB_ID...`, `artifacts VIDEO [--verify]`; `check`
  now also reports FFmpeg, the chosen H.264 encoder and missing encoders or filters.
- Settings: `IFTYEDITS_FFMPEG_PATH`, `IFTYEDITS_FFMPEG_TIMEOUT_S`,
  `IFTYEDITS_PROXY_ENCODER` (`.env.example` updated).

### Acceptance criteria

| Criterion | Evidence |
|---|---|
| Jobs survive application/worker restarts | `test_stale_worker_cannot_overwrite_new_owner`, `test_requeue_honours_cancellation`, `test_startup_marks_interrupted_stages_and_cleans_old_partials`, 1A `test_worker_interrupt_requeues_job` |
| UI remains responsive during processing | FFmpeg runs only in the worker process; pages read SQLite in fragments refreshed every 2 s (`test_ui_processing.py`) |
| Jobs show progress and useful errors | `test_run_reports_progress`, `test_jobs_page_cancels_running_job_and_lists_errors`, `test_library_shows_stage_errors`, `test_process_failure_is_recorded_and_other_stages_finish` |
| Cancellation works safely | `test_cancel_during_proxy_keeps_completed_audio`, `test_real_ffmpeg_cancel_and_timeout`, `test_run_kills_process_that_ignores_sigterm`, `test_cancelling_a_running_job_stops_it`, `test_cancelling_an_import_removes_the_partial_copy`; manual run below |
| Audio and proxy outputs are validated | `test_full_preparation_outputs_and_originals`, `test_proxy_shapes`, `test_failed_validation_leaves_no_partial_and_no_artifact` |
| Re-running a completed stage reuses valid cached output | `test_rerun_reuses_valid_outputs_and_repairs_damaged_ones` |
| Integration tests verify source preservation | `test_full_preparation_outputs_and_originals`, `test_worker_imports_and_prepares_end_to_end`, `test_derived_symlink_into_vault_is_refused` |

### Files

New: `src/iftyedits/storage/migrations/0003_processing.sql`,
`src/iftyedits/storage/processing.py`, `src/iftyedits/adapters/ffmpeg.py`,
`src/iftyedits/services/media_prep.py`, `app/views/jobs.py`, `tests/unit/test_job_lifecycle.py`,
`tests/unit/test_processing_db.py`, `tests/unit/test_ffmpeg_adapter.py`,
`tests/unit/test_media_prep.py`, `tests/unit/test_worker_processing.py`,
`tests/unit/test_ui_processing.py`, `tests/integration/test_media_prep_ffmpeg.py`.

Modified: `src/iftyedits/{cli,config}.py`, `core/{models,errors,protocols}.py`,
`pipeline/{jobs,worker}.py`, `ui/formatting.py`, `app/Home.py`,
`app/views/{library,import_videos,overview}.py`, `.env.example`, `tests/unit/test_cli.py`,
`tests/unit/test_config.py`, `tests/integration/test_import_ffmpeg.py` (expects schema v3),
`docs/{ROADMAP,DECISIONS,PROGRESS}.md`.

### Dependencies

None added. FFmpeg and ffprobe (already required) are used through subprocess.

### Commands and results

Local, 2026-11-20, M1 Max, Homebrew FFmpeg 9.0.2 (VideoToolbox verified):

```text
uv lock --check                              Resolved 52 packages
uv run ruff check .                          All checks passed!
uv run ruff format --check .                 58 files already formatted
uv run mypy                                  Success: no issues found in 51 source files
uv run pytest -m "not ffmpeg and not slow"   246 passed, 25 deselected
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -m "ffmpeg or slow"   25 passed, 246 deselected
uv run pytest                                271 passed
same fast suite with FFmpeg removed from PATH 246 passed (24 ffmpeg tests skip without it)
```

All 177 Phase 1A tests still pass; one assertion changed from `schema v2` to `schema v3`.
Phase 1B added 94 tests (82 fast, 12 FFmpeg).

Manual end-to-end run (temporary data directory, generated media): five videos (1080p60
with audio, display-rotated, odd-sized FFV1, VFR, silent) imported with `enqueue --process`
and prepared by `iftyedits worker --once`; all stages succeeded, `artifacts --verify`
reported OK for every file and the source files' SHA-256 were unchanged. A second run
reused every output. A 4-minute 1080p proxy job (libx264) was cancelled with
`iftyedits cancel` at about 22%: the job ended `cancelled`, the proxy stage `cancelled`, no
FFmpeg process or `*.partial` file remained and no artifact was registered.

### Known limitations

- HDR sources are converted to SDR without tone mapping (no `zscale` in the installed
  FFmpeg); proxies of HDR footage look flat.
- Non-square-pixel (anamorphic) sources are scaled by their stored size, so their proxies
  and thumbnails are not corrected to display aspect.
- Only the first audio stream is extracted.
- Contact sheets are deferred to Phase 3C (see ROADMAP plan changes).
- Failed stages are not retried automatically; queue the video again (valid outputs are
  reused).
- No in-app video playback of proxies yet (Phase 3C).
- One job runs at a time; cancellation takes effect within about one heartbeat (2 s) plus
  FFmpeg shutdown.
- `README.md`, `docs/USER_GUIDE.md` and `docs/TESTING.md` described Phase 1A only until
  2026-11-21; they were updated for Phases 1B and 1C during the Phase 1C session.

### Remaining work

- Owner review of the Phase 1B diff; decide how to commit it relative to PR #1.
- CI run on the Phase 1B branch (Ubuntu FFmpeg 6.1 uses the libx264 path).
- ~~Update README, USER_GUIDE and TESTING for Phase 1B~~ (done 2026-11-21).

### Next recommended task

Phase 1C was implemented on top of this work on 2026-11-21 (see below). Review and commit
Phases 1B and 1C.

---

## Phase 1C — Transcription

**Status:** IN PROGRESS. Implementation complete and verified locally (2026-11-21); every
acceptance criterion has evidence below except a full benchmark: only `tiny` and `base` were
measured, because the network was too slow to download `small` and larger models (D16). The
phase is marked COMPLETE after owner review, the remaining benchmark, commit, a green CI run
and merge (it depends on Phases 1A and 1B, which are not merged).

Work is uncommitted on branch `feat/phase-1b-media` (HEAD `0089f00`), on top of the uncommitted
Phase 1B changes. Nothing was committed or pushed.

### Audit at the start of Phase 1C (2026-11-21)

- Branch `feat/phase-1b-media` at `0089f00`, the same commit as local `feat/phase-1a-import`
  (one commit ahead of `origin/feat/phase-1a-import` at `9d4991d`): the 1B branch was created
  from the 1A branch and all 1B work is uncommitted. PR #1 is still OPEN (head `9d4991d`).
- `CLAUDE.md` has staged and unstaged changes (the rewritten project instructions); left as is.
- Baseline before any Phase 1C change: `uv sync --locked` OK (52 packages), Ruff, format (59
  files) and mypy (51 files) clean, `pytest` 271 passed. Homebrew FFmpeg 9.0.2, VideoToolbox
  verified; `subtitles`/`ass`/`drawtext` still missing (irrelevant to 1C).
- Phase 1B implementation inspected (jobs, worker, media preparation, processing storage, UI):
  it matches the description above.

### Completed tasks

- 1C.1 Provider-neutral interface: `SpeechTranscriber` and `SpeechModelStore` protocols
  (`core/protocols.py`); domain models `TranscriptionOptions`, `TranscriptSegment`,
  `TranscriptWord`, `EngineTranscription`, `Transcript`, `StoredSegment`,
  `TranscriptSearchHit`, `SpeechModelSpec`, `ModelStatus` (`core/models.py`); language list
  (`core/languages.py`, checked against the engine); validation and normalisation of engine
  output (`core/transcript.py`). Language: automatic detection or any of the 100 Whisper codes;
  word timestamps when the engine provides them.
- 1C.2 Local engine: faster-whisper 1.2.1 adapter (`adapters/faster_whisper.py`), CPU int8,
  lazy import, cached model, cancellation and time limit between segments, clear errors for a
  missing package, damaged model or inference failure. Model catalog pinned to Hugging Face
  revisions with sizes and SHA-256 (`adapters/model_store.py`); explicit, verified downloads
  with free-space check, progress and cancellation; configurable default model (D15, D16, D18).
  Benchmark script `scripts/benchmark_transcription.py`.
- 1C.3 Persistence and exports: migration `0004_transcripts` (transcripts, segments, words,
  FTS5 search with triggers); `storage/transcripts.py` (atomic save with ownership check,
  reuse/replace by parameter hash, one current transcript per video, search); exports SRT, VTT,
  TXT and JSON (`services/subtitles.py`) (D17).
- Service `services/transcription.py`: checks, reuse of the Phase 1B WAV (regenerated when
  missing or damaged), WAV header validation, progress, normalisation, storage.
- Jobs and worker: `transcribe_video` and `download_model` job kinds with de-duplication,
  progress, cancellation and the existing ownership/recovery rules.
- UI: new **Transcripts** page (models with sizes and downloads, transcribe with model and
  language, progress and cancel, failure and retry hint, transcript versions, segment and word
  tables, four downloads, search); **Transcript** column in the Library; **Transcribed** count
  on the Overview; new job kinds on the Jobs page.
- CLI: `models [list|download|verify]`, `transcribe`, `transcript [--format] [--save]`,
  `search`; `check` reports the engine and installed models.
- Settings: `IFTYEDITS_TRANSCRIPTION_{MODEL,LANGUAGE,COMPUTE_TYPE,CPU_THREADS,BEAM_SIZE,VAD,
  WORD_TIMESTAMPS,TIMEOUT_S}` (`.env.example` updated).

### Acceptance criteria

| Criterion | Evidence |
|---|---|
| A user can transcribe an imported video locally | Real faster-whisper `tiny`: `test_real_model_transcribes_speech` (opt-in, passed); manual UI run below (Transcripts page → Transcribe → transcript shown); `test_transcription_job_succeeds`, `test_transcribe_button_queues_a_job` |
| Transcript and timestamps persist | `test_save_and_read_back`, `test_transcribes_and_stores`, `test_same_parameters_replace_other_parameters_add`, `test_constraints_reject_invalid_timings`, `test_normalize_*`; real-model test checks every segment and word lies inside the audio |
| Search works | `test_search_matching_rules`, `test_search_only_uses_current_transcripts`, `test_deleting_video_cascades_and_cleans_search`, `test_transcript_viewer_downloads_and_search`, CLI `test_transcript_print_save_and_search`; manual UI search below |
| SRT/VTT exports are valid | `test_srt_is_well_formed`, `test_vtt_is_well_formed_and_escaped`; FFmpeg's own demuxers read both (`subrip`, `webvtt`, cue counts match) in `test_transcription_uses_prepared_audio_and_preserves_original` |
| Model failures and missing models are clearly explained | `test_missing_and_unknown_models_are_explained`, `test_missing_model_fails_with_instructions`, `test_load_failure_is_explained`, `test_inference_failure_is_wrapped`, `test_missing_package_is_explained`, `test_bad_downloads_are_discarded`, `test_network_errors_are_explained`; a real truncated download of `small` failed with "model.bin of model small is incomplete (11669790 of 483546902 bytes). Try again." and left nothing behind |
| Benchmark results are documented | D16: `tiny` and `base`, CPU threads 0/4/8, CPU vs experimental MPS. **Partial**: `small` and larger models and mlx-whisper not measured (bandwidth) |

Other required behaviour: cancellation (`test_cancelling_a_running_transcription`,
`test_cancellation_stores_nothing`, `test_cancellation_between_segments`,
`test_cancelled_download_leaves_nothing`), lost ownership (`test_lost_ownership_stores_nothing`,
`test_save_requires_ownership`), retry (`test_engine_error_fails_job_and_retry_succeeds`),
no audio (`test_video_without_audio_is_refused`, unit and FFmpeg), corrupt or wrong-format audio
(`test_corrupt_or_wrong_audio_is_refused`), damaged WAV regenerated from the original and source
hashes unchanged (`test_transcription_uses_prepared_audio_and_preserves_original`), silence and
zero-length audio (`test_silence_gives_an_empty_transcript_without_language`,
`test_zero_length_audio_skips_the_engine`), malformed segments
(`test_malformed_engine_segments_are_dropped`), timeout (`test_deadline`).

### Files

New: `src/iftyedits/core/{transcript,languages}.py`,
`src/iftyedits/adapters/{faster_whisper,model_store}.py`,
`src/iftyedits/services/{transcription,subtitles}.py`, `src/iftyedits/storage/transcripts.py`,
`src/iftyedits/storage/migrations/0004_transcripts.sql`, `app/views/transcripts.py`,
`scripts/benchmark_transcription.py`, `tests/transcription_fakes.py`,
`tests/unit/test_{transcript_core,transcripts_db,model_store,transcription_service,worker_transcription,cli_transcription,ui_transcripts,faster_whisper_adapter}.py`,
`tests/integration/test_transcription_ffmpeg.py`.

Modified: `pyproject.toml` and `uv.lock` (faster-whisper, `model` marker, mypy override),
`.env.example`, `src/iftyedits/{cli,config}.py`, `core/{models,errors,protocols}.py`,
`pipeline/{jobs,worker}.py`, `storage/processing.py` (shared `assert_job_owned`),
`ui/formatting.py`, `app/Home.py`, `app/views/{library,overview,jobs}.py`,
`scripts/check_env.py`, `tests/unit/test_processing_db.py` and
`tests/integration/test_import_ffmpeg.py` (expected schema version 3 changed to 4), and
`README.md`, `docs/{ROADMAP,PROGRESS,DECISIONS,TESTING,USER_GUIDE}.md`.

### Dependencies

- `faster-whisper>=1.2.1` (runtime; resolved 1.2.1). Brings `ctranslate2` 4.9.0, `tokenizers`,
  `onnxruntime` 1.32.0, `av` 19.0.2, `huggingface-hub`, `hf-xet`, `tqdm` and their
  dependencies (68 packages in `uv.lock`, up from 52). MIT-licensed engine and weights. Why:
  D15.

### Commands and results

Local, 2026-11-21, M1 Max, macOS 26.2, FFmpeg 9.0.2, faster-whisper 1.2.1, CTranslate2 4.9.0:

```text
uv sync --locked                             Checked 66 packages
uv lock --check                              Resolved 68 packages
uv run ruff check .                          All checks passed!
uv run ruff format --check .                 78 files already formatted
uv run mypy                                  Success: no issues found in 70 source files
uv run pytest                                363 passed, 1 skipped (the opt-in model test)
uv run pytest -m "not ffmpeg and not slow"   336 passed
IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest -m "ffmpeg or slow"   27 passed, 1 skipped
IFTYEDITS_RUN_MODEL_TESTS=1 uv run pytest -m model             1 passed (model tiny)
uv run python scripts/check_env.py           OK (faster-whisper 1.2.1, ctranslate2 4.9.0)
```

Phase 1C added 93 tests (364 collected, up from 271). All 271 earlier tests still pass; two
of them assert the schema version and now expect 4 instead of 3.

The real-model test found a real incompatibility: faster-whisper 1.2.1 decodes files with
`av.open(..., metadata_errors=...)`, which PyAV 19.0.2 rejects. The adapter now decodes the
WAV itself and passes a float32 array (D15).

Manual end-to-end run (scratch data and models folders, not the repository's `data/`): a
10.8 s video with synthesised speech and a silent clip were imported and prepared by the
worker; `tiny` and `base` were downloaded through the app (the `tiny` download was started
with the UI's Download button) and verified (`models verify`: OK). On the Transcripts page,
Transcribe queued job 6, the worker ran it and the page refreshed to show 4 segments, 33
words, English (99%), with Start/End times; the silent clip showed "This video has no audio
track". Search for "money" in All videos highlighted the match at 0:07.860.
`transcript 1 --format srt` printed valid cues, `check` reported schema v4 and the installed
models, and `show 1 --verify` reported `integrity OK` with the same SHA-256 as the source file.

Owner verification on real data (2026-11-21): the owner imported a real 15:06 commentary
video through the app's Import page and transcribed it with `tiny` on the Transcripts page.
Result: 163 segments, 2,905 words, English (100%), 28.2 s processing (32.2x real time).
`tiny` mistakes were visible (for example "Grand Peft Auto", "GGA" for GTA); `small` could
not be downloaded on the owner's network at the time.

### Known limitations

- CPU only. CTranslate2's experimental Metal (`mps`) device was measured slower than CPU int8
  for `tiny` and `base` and is not used; no GPU acceleration is claimed.
- mlx-whisper was not benchmarked (D15); larger models were not benchmarked (D16).
- One language per transcript; code-switching is transcribed in the detected language. No
  speaker separation.
- Cancellation and the time limit take effect between segments; one long segment cannot be
  interrupted.
- FTS5 `unicode61` search does not segment Chinese, Japanese or Thai text.
- Downloads cannot resume. Installed-model checks compare file sizes; `models verify` re-hashes.
- Upgrading faster-whisper does not invalidate stored transcripts (the engine version is
  recorded, but not part of the reuse key).
- Transcripts of a deleted video are removed with it (cascade); videos cannot be deleted from
  the app yet.

### Remaining work

- Owner review of the Phase 1B and 1C diff; decide how to commit it relative to PR #1.
- Benchmark `small` (the default) and preferably `large-v3-turbo` with
  `scripts/benchmark_transcription.py`, ideally on a real recording with a hand-checked
  reference; update D16 and, if needed, the default model.
- Optional: compare mlx-whisper in a throwaway environment (D15).
- CI run: the lock now includes faster-whisper and its wheels for macOS and Linux.

### Next recommended task

After review: download `small` (`uv run iftyedits models download small`), run the benchmark
and record it in D16, then commit 1B/1C and get CI green. Phase 2A (content signals) only
after that.

---

## Phase 2 — Clip Intelligence (2A signals, 2B candidates, 2C scoring)

**Status:** COMPLETE locally against the Phase 2 definition of done; owner review, push and merge still pending (nothing pushed). Deferred items are listed below. Branch `feat/phase-2-clip-intelligence` (created from
`feat/phase-1b-media` at `c1e4e6d`, the local commit of Phases 1B and 1C).

### Audit at the start of Phase 2 (2026-11-21)

- Phases 1B and 1C were uncommitted; with the owner's approval they were committed locally
  as `c1e4e6d` on `feat/phase-1b-media` (with the staged CLAUDE.md). Nothing was pushed.
- Baseline before Phase 2: `uv sync --locked` OK, Ruff and format clean, mypy clean,
  pytest 363 passed and 1 opt-in real-model test skipped.

### Milestones

- [x] 2A content signals: sentence rebuilding, transcript, audio and scene signals, with
  unit and FFmpeg tests.
- [x] 2B candidate generation: anchor, topic-block and thought-start proposals with
  sentence-aligned, scene-snapped boundaries and duplicate merging.
- [x] 2C scoring and ranking (`core/scoring.py`)
- [x] Persistence (migration 0005, `storage/clips.py`) and service (`services/clip_analysis.py`)
- [x] Job kind `analyze_clips`, worker execution, progress, cancellation, idempotent retry
- [x] CLI (`analyze`, `clips ...`) and the Clips page, plus Library and Overview columns
- [x] Documentation (ROADMAP, DECISIONS D19 to D21, USER_GUIDE, TESTING, README)

### Local commits on `feat/phase-2-clip-intelligence` (none pushed)

`2fa35a7` 2A signals, `dd55d83` and `dd99d7e` 2B candidates, `b92c8d9` 2C scoring,
`2429cc9` persistence and service, `86ad555` job and worker, `52faf48` Clips page, CLI and
display helpers, then the documentation commit that follows them.

### Verification (2026-11-22)

- `uv sync --locked`, `ruff check`, `ruff format --check` and `mypy` (93 source files) clean.
- `IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest`: 483 passed, 1 skipped (the opt-in real-model
  test, `IFTYEDITS_RUN_MODEL_TESTS`). Clip-specific tests: signals, candidates, scoring,
  storage, service, worker, CLI, UI helpers and page (AppTest), plus FFmpeg scene tests.
- Dashboard started headless on 127.0.0.1 (health `ok`).
- Real-media smoke test in a scratch folder (never `data/`): a generated 90 s H.264 video was
  imported and prepared by the real worker and FFmpeg (audio, proxy, thumbnail), then
  analysed through the CLI and worker. It produced 3 suggestions from 16 signals. After
  favoriting the top one, re-analysis re-ranked it (best score 72 to 81). **Limitation:** the
  transcript in that run was a scripted fixture, not a real Whisper transcript, because no
  speech model is installed; transcription itself was verified in Phase 1C.

### Limitations and deferred work

- Not implemented: face detection (2A.3), speaker separation, optional language-model
  assistance (2B.3), preview playback and a boundary-adjust screen (database fields exist).
- Wording cues are English only. Scores are algorithmic recommendations, not predictions.
- Suggestion quality on real long-form speech is not yet benchmarked by the owner.

### Next recommended task

Owner review of the Phase 2 diff and a try on a real transcript (`uv run iftyedits ui`,
Clips page). Then Phase 3A (vertical reframing), which can use `ClipCandidate.effective_start_s`
and `effective_end_s` as its input.

---

## Phase 2 stabilization (2026-11-22)

Triggered by a real-world run on the GTA 6 video. Branch `feat/phase-2-clip-intelligence`.

### Root causes

- **`ImportError: ANALYZE_CLIPS` / `ClipAnalysisError`:** not a code defect. `iftyedits ui`
  (dashboard and worker, pid 67645 onward) was started at 23:19, before the Phase 2 code was on
  disk. Streamlit reloads page scripts but not already imported library modules, so the
  running process kept the old `pipeline.jobs` and `core.errors`. Both names exist on the
  branch (`jobs.py:31`, `errors.py:93`). Fix: restart the app. A test now imports every name the
  pages use.
- **`No video with id 1` / `No videos imported yet`:** same database, no mismatch. The CLI
  command ran at about 23:19, before the video was imported at 23:38. Both the CLI and the
  pages use `Settings.from_env()`; `check` showed the same `data/db/iftyedits.sqlite`. The
  one real hazard is documented: relative paths resolve against the current directory, so a
  CLI run from another folder opens (and creates) a different, empty `data/`.
- **Duplicate `transcribe_video` jobs:** job 4 was a second click; it was reused
  (`Reused existing tiny transcript`) in 0.05 s, as designed.
- **Job 5 (`download_model` small):** not stuck. It is a live third attempt, heartbeating, in
  a `.download-small-*` staging folder, left alone. The earlier failures were CLI downloads.

### Changes

- `adapters/model_store.py`: retry up to 4 times with `Range` resume, re-hash the partial
  file, restart when the server ignores `Range`, treat `IncompleteRead` and other `OSError`s
  as transient (they used to escape as raw tracebacks). Never marks a partial model installed.
- Tests: `tests/unit/test_model_store.py` (resume, ignored range, give-up, 5xx versus 404),
  `tests/unit/test_ui_pages_consistency.py` (every page runs, every imported name exists, job
  kinds and clip errors importable, CLI and pages share a database, late import visible).

### Real GTA 6 validation

Real transcript (tiny, 163 segments, 2,905 words), real 15:06 video, real data folder, no
fixture. Result: 168 signals, 49 proposals, 20 suggestions, scores 45 to 63, analysis 9.8 s.
Boundaries land on sentence edges and every clip is 13 to 61 s. Known weaknesses found: the
closing "like and subscribe" outro ranks #5 and the host's intro line anchors #4 (no
intro/outro/call-to-action detection yet), and "Emotional moment" or "Funny moment" labels
fire on commentary wording in a gameplay breakdown. Strength and weakness lines can show a
component's detail text (for example "Weak start") under Strengths when its score is high.
Suggested follow-up: a boilerplate/CTA penalty with a `SCORING_VERSION` bump.

