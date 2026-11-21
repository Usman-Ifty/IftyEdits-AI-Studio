# IftyEdits AI Studio — Complete Product Roadmap

## Product outcome

Deliver a reliable local-first application that imports long videos, finds promising moments, creates polished vertical Shorts with captions, allows manual refinement, generates upload metadata, performs a safety review and exports finished videos.

The application should be useful end-to-end. A beautiful interface with nonfunctional controls is not an acceptable outcome.

## Execution rules

- Execute phases in dependency order.
- Reconcile every task against the actual repository before implementation.
- Preserve all existing functionality and tests.
- Use small implementation milestones.
- Add tests with each capability.
- Do not skip a phase's acceptance criteria.
- Do not commit or push without explicit user approval.
- Update `docs/PROGRESS.md` after each completed milestone.
- Record major technology choices in `docs/DECISIONS.md`.
- Prefer local processing and minimal recurring costs.
- Keep all external publishing actions behind explicit human approval.

---

# Phase 1A — Video Import Foundation

**Goal:** Reliably import video files, protect originals, inspect metadata and display an actual media library.

### 1A.1 Architecture and schema
- Reinspect the current repository and existing original vault.
- Design a normalized SQLite schema.
- Add numbered SQL migrations and schema versioning.
- Create records for videos, source locations, media streams and import status.
- Add repository functions with transactions.
- Add migration tests, rollback/error tests and fresh-database tests.
- Avoid storing derived metadata redundantly.

### 1A.2 Configuration
- Improve settings validation.
- Support an explicit application data directory such as `IFTYEDITS_HOME`.
- Support `.env` only through a correctly implemented and tested settings mechanism.
- Update `.env.example`.
- Never commit secrets.
- Keep project code and user media directories separate.

### 1A.3 FFprobe adapter
- Build a typed FFprobe adapter.
- Parse duration, dimensions, codec, pixel format, frame rates, time base, bitrate, audio streams and metadata.
- Handle rotation/display matrices.
- Distinguish average and nominal frame rates.
- Handle VFR, missing audio, missing metadata and malformed media.
- Capture HDR/color metadata where available.
- Return clear domain errors for unsupported or corrupt input.
- Test with generated media and representative metadata fixtures.

### 1A.4 Ingestion service
- Validate source paths and file types.
- Probe before importing so non-video files are rejected early.
- Import original bytes into the protected vault.
- Calculate SHA-256 during copying rather than reading the source twice.
- Verify copied content.
- Handle duplicate content across filenames and extensions.
- Preserve source-location records for duplicates.
- Handle disk-full errors and interrupted imports.
- Handle a database failure after a successful vault copy without losing the original.
- Provide a safe reconciliation mechanism for orphaned vault files.
- Do not modify, move, delete or hard-link source files.

### 1A.5 CLI
- Add commands to inspect environment, import files, import folders, list library entries and inspect a video.
- Provide clear progress and error messages.
- Support paths containing spaces and Unicode.
- Do not silently skip files in folder imports.

### 1A.6 Streamlit library and import UI
- Build an actual library view backed by SQLite.
- Support file and folder path import workflows.
- Avoid buffering huge files through Streamlit's default uploader.
- Show import status, thumbnail if available, filename, duration, resolution, codecs and timestamps.
- Show duplicates and errors clearly.
- Add a video details page.
- Make controls functional.

### Phase 1A acceptance criteria
- Import MP4 and MOV successfully.
- Reject unsupported or corrupt files with useful errors.
- Extract correct metadata.
- Persist records in SQLite.
- Detect duplicates by content hash.
- Preserve source bytes and hash.
- Handle interrupted import and DB failure safely.
- Show imported media in the UI.
- All tests, lint, formatting and typing checks pass.

---

# Phase 1B — Media Preparation and Job System

**Goal:** Process imported videos reliably without blocking the UI.

### 1B.1 Job architecture
- Add persistent jobs with status, stage, progress, timestamps, attempts and error details.
- Support queued, running, succeeded, failed and cancelled states.
- Add job ownership/locking to prevent duplicate workers.
- Add retry rules for recoverable failures.
- Make stages restartable and idempotent.
- Record stage outputs and parameters.
- Avoid executing heavy work in Streamlit request/page lifecycle.

### 1B.2 Worker
- Implement a separate worker process.
- Add graceful shutdown and cancellation.
- Recover stale jobs after a crash.
- Ensure a failed stage does not corrupt previous outputs.
- Expose progress to the UI.

### 1B.3 Audio extraction
- Extract normalized mono 16 kHz WAV for transcription.
- Handle videos without audio.
- Verify output format and duration.
- Preserve source media.
- Record extraction parameters and output hash.

### 1B.4 Proxy and thumbnail generation
- Generate lightweight preview proxies, targeting 720p where suitable.
- Generate representative thumbnails and contact sheets.
- Detect and handle rotation, VFR, HDR and unusual codecs.
- Use VideoToolbox only when detected and verified; otherwise provide a reliable software fallback.
- Use predictable cache paths and stage/version/parameter hashes.
- Do not overwrite unrelated outputs.

### Phase 1B acceptance criteria
- Jobs survive application/worker restarts.
- UI remains responsive during processing.
- Jobs show progress and useful errors.
- Cancellation works safely.
- Audio and proxy outputs are validated.
- Re-running a completed stage reuses valid cached output.
- Integration tests verify source preservation.

### Phase 1B plan changes (2026-11-20)
- Contact sheets (1B.4) are deferred to Phase 3C (editor and preview), where a strip of
  frames is useful for trimming. Phase 1B produces one representative thumbnail per video.
- HDR sources are converted to SDR without tone mapping; the installed FFmpeg lacks
  `zscale`. Revisit if HDR footage becomes common (see D14).
- Automatic retries (1B.1) apply to jobs interrupted by a worker crash or shutdown. Failed
  stages are retried by queuing the video again; valid outputs from earlier runs are reused.

---

# Phase 1C — Transcription

**Goal:** Produce accurate searchable transcripts with timestamps.

### 1C.1 Transcription interface
- Define a provider-neutral transcription protocol.
- Support language selection and automatic detection.
- Support word-level timestamps where the engine provides them.
- Normalize provider output into internal segment and word models.

### 1C.2 Local engine
- Evaluate faster-whisper models on Apple Silicon.
- Benchmark CPU inference and compare an MLX-based alternative if practical.
- Compare speed, memory, accuracy and installation complexity.
- Keep optional model dependencies isolated.
- Do not install multiple large frameworks without evidence they are needed.
- Make model selection configurable.
- Show model download size and required disk space before download.

### 1C.3 Persistence and exports
- Persist transcript segments and word timestamps.
- Link transcript records to source and processing version.
- Support transcript search.
- Export SRT and VTT.
- Handle punctuation, overlapping speech, multilingual text and empty transcripts.

### Phase 1C acceptance criteria
- A user can transcribe an imported video locally.
- Transcript and timestamps persist.
- Search works.
- SRT/VTT exports are valid.
- Model failures and missing models are clearly explained.
- Benchmark results are documented.

### Phase 1C plan changes (2026-11-21)
- faster-whisper is the only engine (D15). The MLX comparison (1C.2) was not run: mlx-whisper
  needs PyTorch, and the available bandwidth on 2026-11-21 made a second framework and second
  set of weights impractical. It stays a follow-up, run in a throwaway environment so the
  project lock does not change.
- The benchmark covers the models that could be downloaded during the phase; larger models are
  listed in D16 as not measured. `scripts/benchmark_transcription.py` reproduces it.
- Model sizes are shown from a pinned catalog (sizes and SHA-256 from the Hugging Face API);
  downloads are explicit jobs that check free space and verify files (D18).
- Run state (progress, errors, cancellation, retries) is tracked on the job; only finished
  transcripts are stored (D17). A failed transcription is retried by starting it again.
- Overlapping speech is stored as overlapping segments; there is no speaker separation.

---

# Phase 2A — Content Signals

**Goal:** Extract useful signals that help locate interesting moments.

### 2A.1 Scene detection
- Integrate PySceneDetect or a justified alternative.
- Detect scene boundaries and persist timestamps.
- Provide configurable sensitivity.
- Avoid excessive frame sampling.

### 2A.2 Audio signals
- Analyze loudness, peaks, silence and speech activity.
- Identify potentially expressive moments such as sudden volume changes.
- Do not equate loudness with quality.
- Persist explainable measurements.

### 2A.3 Visual signals
- Evaluate OpenCV built-in face detection such as YuNet.
- Detect faces and track approximate positions over time.
- Handle no-face, multiple-face and low-confidence cases.
- Keep face detection optional and replaceable.
- Do not identify real people.

### 2A.4 Timeline
- Combine transcript, scene, audio and visual signals on a common timeline.
- Make timestamps inspectable.
- Show confidence and signal source.

### Phase 2A implementation status (2026-11-22)

Implemented: transcript signals (question and answer, story, hook, insight, motivation,
emotional and humorous wording, surprise, topic shifts, rapid exchange, pauses, speech
density), audio loudness peaks and silence from the prepared WAV, and FFmpeg scene changes
from the proxy (D20). Every signal records its source (transcript, audio or video), time
span, strength and reliability. Wording cues are English only; other languages are marked
"not assessed". **Deferred:** face detection (2A.3, OpenCV/YuNet) and speaker separation.
The application never claims to have analysed faces or speakers (the details panel says so).

### Phase 2A acceptance criteria
- Signals are persisted and reproducible.
- Timeline data aligns with source timestamps.
- No-face and no-audio cases work.
- Tests cover edge cases.
- The UI explains why a signal was detected.

---

# Phase 2B — Moment and Clip Candidate Generation

**Goal:** Find promising, self-contained moments rather than arbitrary chunks.

### 2B.1 Candidate generation
- Generate candidates from transcript segments and word timestamps.
- Support configurable target duration, initially 15–60 seconds.
- Prefer complete thoughts, punchlines, reactions, useful explanations and story beats.
- Snap boundaries intelligently to sentence and scene boundaries.
- Avoid cutting off words or context.
- Support overlapping candidates when justified.

### 2B.2 Candidate categories
- Conversation/podcast insight.
- Funny or surprising moment.
- Reaction or emotional moment.
- Gaming highlight.
- Challenge or reveal.
- Motivational or educational moment.
- Story or narrative payoff.

These are suggestions, not hard-coded assumptions about every source.

### 2B.3 Optional language model assistance
- Use a provider interface.
- Default to local processing when practical.
- Make cloud APIs optional and opt-in.
- Never upload private video/transcript content to a provider without clear user consent.
- Treat model output as untrusted suggestions.
- Validate timestamps against the actual source duration.
- Handle hallucinated quotes and inaccurate summaries.

### Phase 2B implementation status (2026-11-22)

Implemented: sentence-aligned candidates grown from signals, topic blocks and thought starts,
with scene-snapped boundaries, duplicate merging and hook, context and payoff spans.
Categories are listed in `core/scoring.py` (`CATEGORY_LABELS`). The user can favorite or
reject a suggestion (Clips page and CLI). **Deferred:** optional language-model assistance
(2B.3), in-app preview (needs the playback of Phase 3C) and a screen to adjust boundaries
(the database already stores `adjusted_start_s` and `adjusted_end_s` for it).

### Phase 2B acceptance criteria
- Candidate clips are generated from actual source timestamps.
- Each candidate includes a transcript excerpt and explanation.
- Durations and boundaries are valid.
- Candidates can be previewed.
- Users can accept, reject and adjust candidates.

---

# Phase 2C — Explainable Clip Scoring

**Goal:** Rank candidate clips with transparent reasoning.

Build a configurable scoring model using signals such as:
- Completeness of thought.
- Hook strength.
- Emotional or expressive change.
- Speech activity.
- Scene changes.
- Audio emphasis.
- Clarity of context.
- Ending/payoff.
- Duration suitability.
- Repetition or dead-air penalties.

Requirements:
- Expose score components and explanations.
- Avoid claiming that a score predicts virality.
- Do not optimize only for loudness or sensational content.
- Allow user feedback to influence future ranking.
- Keep the scoring algorithm deterministic and testable.
- Add baseline fixtures to detect regressions.

### Phase 2C implementation status (2026-11-22)

Implemented: deterministic, versioned scoring (`SCORING_VERSION` 1) with 12 weighted
components, penalties and an evidence-quality confidence; greedy overlap-aware ranking;
feedback (favorite, reject, per-type bias) that changes the next ranking. Scores are
recommendations, not predictions. Fixture-based regression tests cover scoring.

### Phase 2C acceptance criteria
- Candidates have explainable component scores.
- Ranking is stable for identical inputs.
- User feedback is stored.
- Tests cover scoring and tie-breaking.
- UI explains why clips rank highly.

---

> **Status (2026-11-22):** Phases 3A-3C were delivered together as one slice; see D24 and
> PROGRESS.md. Not built: face-aware reframing, transitions, contact sheets.

# Phase 3A — Vertical Reframing

**Goal:** Convert landscape source clips into polished 9:16 compositions.

- Build a crop/reframe service.
- Support center crop.
- Support face-guided crop when reliable face tracks exist.
- Smooth crop movement to avoid jitter.
- Support multiple faces and scene changes.
- Fall back gracefully when face tracking is unavailable.
- Offer blurred-background padding as an alternative.
- Keep subject safe areas in mind.
- Handle source rotation and aspect ratios.
- Preview crop decisions before export.

### Phase 3A acceptance criteria
- Output framing is 9:16.
- Crop stays within source bounds.
- Face-guided framing is optional and explainable.
- No-face fallback works.
- Crop movement is smooth.
- Tests cover portrait, landscape, square and rotated media.

---

# Phase 3B — Captions

**Goal:** Produce accurate, readable and visually polished captions.

- Build caption generation from word timestamps.
- Support phrase-based caption grouping.
- Provide configurable font, size, position, color, outline and background.
- Support highlighted words and emphasis.
- Keep text within safe margins.
- Support Urdu and other Unicode text where fonts are available.
- Use Pillow-rendered transparent overlays as the default if FFmpeg lacks the necessary subtitle filters.
- Verify font availability and fallback behavior.
- Avoid relying on unavailable libass/freetype/drawtext features.
- Export SRT/VTT independently of burned-in captions.

### Phase 3B acceptance criteria
- Captions align with speech.
- Text remains readable at mobile size.
- Unicode text renders correctly with configured fonts.
- Caption styles are reusable.
- Caption timing and layout can be edited.
- Tests cover line wrapping and timing.

---

# Phase 3C — Clip Editor and Preview

**Goal:** Let the user refine generated clips before export.

- Build a clip list and timeline.
- Edit in/out points.
- Edit title/description metadata.
- Edit transcript and caption text.
- Adjust crop and framing.
- Select caption style.
- Preview the actual rendered result or a faithful proxy preview.
- Support undo/redo where practical.
- Save edits persistently.
- Prevent edits from modifying original media.
- Show processing status and preview limitations.

### Phase 3C acceptance criteria
- User can refine a candidate without changing the source.
- Edits persist after restart.
- Preview reflects selected edits.
- Invalid time ranges are rejected.
- Export uses the saved edit configuration.

---

# Phase 4 — SEO and Publishing Metadata

**Goal:** Generate useful YouTube metadata for each selected Short.

- Generate multiple title suggestions.
- Generate concise descriptions.
- Suggest relevant hashtags and tags.
- Support creator/channel/source attribution fields.
- Use transcript and actual clip context.
- Avoid fabricated facts, names, claims or quotes.
- Avoid keyword stuffing and misleading clickbait.
- Keep the user in control of final text.
- Support templates and saved channel preferences.
- Default to a local model/provider where practical.
- Make external LLM APIs optional, configurable and cost-aware.
- Never send transcripts or private content externally without consent.

### Phase 4 acceptance criteria
- Metadata can be generated from a selected clip.
- Suggestions are editable.
- Output is grounded in the transcript/context.
- No API key is required for the core local workflow.
- Tests verify formatting and missing-data handling.

---

# Phase 5 — Export and Batch Processing

**Goal:** Produce upload-ready vertical videos.

### Export
- Default target: 1080x1920, 9:16.
- H.264 video and AAC audio unless a justified alternative is selected.
- Use suitable frame rate and pixel format.
- Normalize audio sensibly; target approximately -14 LUFS only as a configurable starting point, not a universal rule.
- Verify duration, dimensions, audio and codec after export.
- Support caption-burned and clean exports.
- Use safe filenames and collision handling.
- Keep export paths configurable.
- Preserve project/edit metadata.

### Batch
- Export multiple selected clips.
- Queue work through the worker.
- Show per-item and overall progress.
- Allow safe cancellation.
- Recover from failed items without corrupting successful exports.

### Phase 5 acceptance criteria
- Exported video passes FFprobe validation.
- Export is playable.
- Correct vertical dimensions and audio are verified.
- Batch failures are isolated.
- Source files remain unchanged.
- Output is stored in the configured export directory.

---

# Phase 6 — Safety, Rights and Policy Review

**Goal:** Help the user review content before publishing.

Create a review checklist and automated warning system for:
- Potential profanity and offensive language.
- Personal information and sensitive details.
- Harassment or targeted abuse.
- Dangerous or harmful instructions.
- Defamation or misleading claims.
- Copyrighted music and third-party audio.
- Third-party footage and reused content.
- Context loss from trimming.
- Attribution and permission records.
- Platform policy concerns.

Important limitations:
- Automated checks are advisory.
- They cannot determine legal fair use.
- They cannot guarantee permission.
- They cannot guarantee YouTube policy compliance.
- They cannot reliably predict Content ID claims.
- They cannot guarantee monetization eligibility.

Require a human review and explicit approval before a clip can enter the publishing workflow.

### Phase 6 acceptance criteria
- Every export can have a safety review record.
- Warnings include reasons and timestamps where possible.
- User can mark reviewed items.
- Unreviewed clips are visibly distinguished.
- The application never labels a clip "copyright-free" based on automated analysis.

---

# Phase 7 — Optional YouTube Integration

**Goal:** Support controlled publishing without removing human authority.

- Research current official YouTube Data API requirements.
- Implement OAuth securely only when needed.
- Store tokens outside the repository with restrictive permissions.
- Never log tokens.
- Respect quota limits.
- Support upload as private/unlisted draft where supported.
- Require explicit approval of the exact video, title, description, visibility and destination channel before upload.
- Never publish or schedule automatically.
- Track upload status and errors.
- Provide a manual export workflow if OAuth is not configured.

### Phase 7 acceptance criteria
- OAuth is optional.
- Tokens are protected.
- User sees the exact upload details before approval.
- No upload occurs without explicit approval.
- Errors and quota limits are handled.
- Manual export remains available.

---

# Phase 8 — Analytics and Feedback

**Goal:** Help the creator understand performance and improve selection.

- Research official YouTube Analytics API capabilities and permissions.
- Make connection optional.
- Store imported analytics snapshots.
- Track available metrics such as views, retention, engagement and traffic sources where authorized.
- Link analytics to exported/uploaded clips.
- Compare performance across clip categories and editing choices.
- Avoid claiming causation from small samples.
- Use user feedback and performance data to tune ranking transparently.
- Never change publishing behavior automatically.

### Phase 8 acceptance criteria
- Analytics can be imported with user consent.
- Metrics are linked to the correct video.
- Comparisons show sample sizes and time windows.
- Recommendations explain their basis.
- No automatic publishing or destructive optimization occurs.

---

# Phase 9 — Product Quality and Release Readiness

**Goal:** Turn the implementation into a maintainable daily-use application.

- Review the full application architecture.
- Remove dead code and duplicated logic.
- Audit error handling and security boundaries.
- Add logging and diagnostics.
- Improve installation and first-run setup.
- Add user documentation and troubleshooting.
- Verify clean setup from a fresh checkout.
- Verify all essential workflows on Apple Silicon macOS.
- Test missing FFmpeg, missing models and insufficient disk space.
- Add CI integration coverage where practical.
- Add packaging/distribution only after local workflows are stable.
- Document licensing implications of FFmpeg and model dependencies.
- Create a release checklist.

### Phase 9 acceptance criteria
- A new checkout can be installed using documented steps.
- A user can complete import-to-export without manual database manipulation.
- Essential tests pass.
- Known limitations are documented.
- No critical placeholder controls remain.
- Release process is reproducible.

---

# Recommended implementation order

1. Phase 1A — Import foundation
2. Phase 1B — Media preparation and jobs
3. Phase 1C — Transcription
4. Phase 2A — Content signals
5. Phase 2B — Candidate generation
6. Phase 2C — Clip scoring
7. Phase 3A — Vertical reframing
8. Phase 3B — Captions
9. Phase 3C — Editor and preview
10. Phase 5 — Export and batch
11. Phase 4 — SEO metadata
12. Phase 6 — Safety review
13. Phase 7 — Optional YouTube integration
14. Phase 8 — Analytics
15. Phase 9 — Release readiness

Phase 5 is intentionally before Phase 4: a reliable rendered Short is more important than metadata generation.

## Progress tracking format

For every phase, record:

- Status: NOT STARTED / IN PROGRESS / BLOCKED / COMPLETE.
- Completed tasks.
- Files created or modified.
- Architecture decisions.
- Dependencies added and why.
- Commands executed.
- Exact test results.
- Known limitations.
- Remaining work.
- Next recommended task.

Never mark a phase COMPLETE until all its acceptance criteria are verified.

