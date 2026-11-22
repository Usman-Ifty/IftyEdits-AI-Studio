# Decisions

Major technology and architecture choices. Newest last. Each entry: context, decision,
consequences. Do not rewrite past entries; add a new one that supersedes them.

## D1 — Python 3.12 managed by uv (2026-11-18)

**Context:** System Python is 3.9.6 from Xcode and must not be changed.
**Decision:** uv-managed CPython 3.12 in a project `.venv`; `uv.lock` committed.
**Consequences:** Reproducible installs (`uv sync --locked`), no system changes.

## D2 — Modular monolith with inward dependencies (2026-11-18)

**Context:** Eleven pipeline stages, one developer machine.
**Decision:** One package with `core` (no I/O), `storage`, `adapters`, `services`,
`pipeline`, `ui`; Streamlit in `app/` stays thin.
**Consequences:** Stages are testable in isolation; FastAPI can reuse services later.

## D3 — Content-addressed, read-only originals vault (2026-11-18)

**Context:** Source media must never be modified, moved, deleted or linked.
**Decision:** Independent byte copies named by SHA-256 under `data/originals/<sha[:2]>/`,
mode `0444`, written via `data/staging/` and moved in under a lock; no hard links;
`chflags uchg` not used by default.
**Consequences:** Dedupe by content, no partial files in the vault, originals survive DB loss.

## D4 — Single-pass copy and hash (2026-11-18)

**Context:** Hash-then-copy doubled reads on large files and external drives.
**Decision:** Hash while copying; reject the import if source size or mtime changes.
A size-match shortcut hashes only when stored content has the same size.
**Consequences:** One read for new files; duplicates are found without a copy.

## D5 — SQLite with numbered SQL migrations, no ORM (2026-11-18)

**Context:** Simple relational data, strict typing, minimal dependencies.
**Decision:** stdlib `sqlite3`, WAL, foreign keys, `NNNN_name.sql` migrations versioned by
`PRAGMA user_version`, explicit repositories.
**Consequences:** No SQLAlchemy/Alembic; migrations must be additive and contiguous.

## D6 — pydantic-settings for configuration (2026-11-18)

**Context:** README promised `.env` support that did not exist; FastAPI will need pydantic.
**Decision:** `Settings` on pydantic-settings, `IFTYEDITS_` prefix, `.env` from
`IFTYEDITS_HOME` (default: current directory).
**Consequences:** One validated settings object; relative paths resolve predictably.

## D7 — SQLite job queue and separate worker process (2026-11-18)

**Context:** Streamlit reruns its script on every interaction; long copies must not run there.
**Decision:** `jobs` table, atomic claim with `UPDATE … RETURNING`, heartbeat thread,
stale-job recovery, requeue on SIGTERM/Ctrl-C; `iftyedits ui` starts a worker alongside Streamlit.
**Consequences:** UI stays responsive; jobs survive restarts. Moved forward from Phase 1B.

## D8 — Probe before copy (2026-11-18)

**Context:** Non-video files should never enter the vault.
**Decision:** ffprobe the source first and require a real video stream; probe the vault copy
again for the stored metadata.
**Consequences:** Two probes per new file (cheap); rejected files leave no trace.

## D9 — Localhost-only UI (2026-11-18)

**Context:** The app exposes local file paths and media.
**Decision:** Streamlit bound to `127.0.0.1` via CLI and `.streamlit/config.toml`; usage
statistics off; no browser uploads for media (path-based import).
**Consequences:** Not reachable from the network by default; large files are never buffered.

## D10 — CI split (2026-11-18)

**Context:** macOS 14 runners were retired; FFmpeg is not on the macOS runner image.
**Decision:** `checks` on `macos-26` (lint, types, unit tests); `integration` on
`ubuntu-24.04` with apt FFmpeg and `IFTYEDITS_REQUIRE_FFMPEG=1`. Actions pinned by SHA.
**Consequences:** FFmpeg tests run in CI and cannot be skipped silently there.

## D11 — Roadmap separate from development rules (2026-11-18)

**Context:** The product roadmap and the permanent engineering rules serve different purposes.
**Decision:** `CLAUDE.md` holds rules for all phases; `docs/ROADMAP.md` holds phases and
acceptance criteria; `docs/PROGRESS.md` tracks status. Rules in `CLAUDE.md` win on conflict.
**Consequences:** Rules stay loaded for every session; the roadmap can evolve freely.

## D12 — Job ownership and cooperative cancellation (2026-11-20)

**Context:** Phase 1B jobs run FFmpeg for minutes. A stalled worker that wakes up after
stale recovery must not overwrite the result of the worker that took over, and the user
must be able to stop a job without corrupting state.
**Decision:** Migration `0003` rebuilds `jobs` (SQLite cannot alter a CHECK constraint)
with a `cancelled` status and `cancel_requested_at`. Every state change is a guarded
`UPDATE ... WHERE status = 'running' AND worker_id = ?`; it returns False when the caller
no longer owns the job. Stage and artifact writes check ownership in the same
transaction. Cancelling a queued job is immediate; cancelling a running job sets a flag
that the worker's heartbeat thread turns into a cancel event. FFmpeg is stopped with
SIGTERM, then SIGKILL after 5 s, and cancellation is reported only after the process has
exited. Stale recovery cancels (rather than requeues) jobs with a pending cancellation.
**Consequences:** Stale workers cannot corrupt results; cancellation latency is about one
heartbeat (2 s) plus FFmpeg shutdown. Superseded D7 in part: `succeed`, `fail` and
`requeue` now only act on running jobs.

## D13 — Processing stages and artifacts (2026-11-20)

**Context:** Outputs must be traceable to their video and job, reusable when valid and
never written over originals.
**Decision:** `processing_stages` keeps the latest state of each (video, stage);
`artifacts` keeps the current file of each (video, kind) with size, SHA-256, parameter
hash, metadata and job id. Output paths are deterministic:
`data/derived/<sha[:2]>/<sha>/<name>-<params_hash>.<ext>`. FFmpeg writes a unique hidden
`*.partial` file next to the final path; the output is probed and validated, hashed and
renamed into place atomically, then registered. A valid artifact with the same parameter
hash is reused unless the run is forced. A failed stage does not stop the other stages.
**Consequences:** No stage history beyond the latest run (job rows keep the history of
runs). Changing stage parameters or `STAGE_VERSION` regenerates outputs at a new path and
removes the superseded file.

## D14 — Proxy, audio and thumbnail formats (2026-11-20)

**Context:** Phase 1C needs speech audio; later phases need fast previews on an M1 Max.
**Decision:** Audio: first audio stream, downmixed to 16 kHz mono PCM s16le WAV. Proxy:
H.264 MP4, shorter side at most 720 px (never upscaled, even dimensions), constant frame
rate equal to the source average capped at 30 fps, keyframe every 2 s, AAC 128 kb/s stereo
when the source has audio, `+faststart`. VideoToolbox (`h264_videotoolbox`, 3 Mb/s) is used
only after a test encode succeeds, otherwise libx264 (`veryfast`, CRF 26); a VideoToolbox
failure during a real encode is retried once with libx264. FFmpeg's default auto-rotation
applies display rotation. Thumbnail: one 640x360 JPEG (letterboxed), chosen by the
`thumbnail` filter from 50 frames starting 10% into the proxy (or the original when no
proxy exists).
**Consequences:** HDR sources are converted to 8-bit SDR without tone mapping (the
installed FFmpeg has no `zscale`), so their proxies look flat. Proxies are for preview and
analysis, not final export.

## D15 — Transcription engine: faster-whisper on the CPU (2026-11-21)

**Context:** Phase 1C needs local speech-to-text with segment and word timestamps, language
detection and selection, on an M1 Max with 32 GB RAM, without recurring cost.
**Options considered** (package metadata checked on PyPI on 2026-11-21):
- **faster-whisper 1.2.1** (MIT; CTranslate2 4.9.0). Wheels for macOS arm64 and Linux on Python
  3.12. Dependencies: `ctranslate2`, `tokenizers`, `onnxruntime` (Silero voice-activity
  detection), `av` (PyAV), `huggingface-hub`, `tqdm`; about 60 MB of wheels, no PyTorch. Word
  timestamps, language detection, VAD, int8 CPU inference. CTranslate2 4.9.0 also ships an
  **experimental** Apple Metal (`mps`) device; it was measured and is slower than CPU int8 for
  `tiny` and `base` on the M1 Max (see D16), so it is not used.
- **mlx-whisper 0.4.3** (MIT). Runs on the Apple GPU through MLX. Requires `torch`, `numba`,
  `scipy`, `tiktoken` and `mlx`, i.e. a full PyTorch install, and Apple Silicon only (no Linux CI).
- whisper.cpp bindings and OpenAI's `openai-whisper` (PyTorch, slow on CPU) were not pursued.
**Decision:** faster-whisper is the only engine, added as a runtime dependency. It is imported
only inside `adapters/faster_whisper.py` (lazily, when a job runs), behind the
`SpeechTranscriber` protocol, so the rest of the app runs and explains the problem if it is
missing, and another engine can be added later without touching services or storage. Device
is always `cpu`; compute type defaults to `int8`. Audio is decoded by the adapter (stdlib
`wave` + numpy, chunked) and passed to faster-whisper as a float32 array: faster-whisper
1.2.1's own PyAV decoder calls `av.open(..., metadata_errors=...)`, which PyAV 19.0.2 rejects
(`TypeError`, found by the opt-in real-model test). mlx-whisper was **not** benchmarked in this
phase: the network on 2026-11-21 delivered roughly 10–700 KB/s, which made a PyTorch install
plus a second set of weights impractical, and adding PyTorch for a comparison is exactly the
"large framework without evidence" the roadmap warns against.
**Consequences:** One engine, CPU only; GPU acceleration is not claimed anywhere. The `mps`
device can be re-measured for larger models later; it is not exposed as a setting. A fair
faster-whisper vs mlx-whisper comparison (same model size, same audio) is an open follow-up:
run `scripts/benchmark_transcription.py` for faster-whisper, and mlx-whisper in a throwaway
environment (`uv run --isolated --with mlx-whisper ...`) so the project lock is not changed.

## D16 — Speech models, defaults and benchmark (2026-11-21)

**Context:** Model choice trades speed against accuracy; downloads must be explicit and
reproducible (roadmap 1C.2).
**Decision:** A fixed catalog (`adapters/model_store.py`): `tiny`, `base`, `small`, `medium`,
`large-v3-turbo`, `large-v3`, each pinned to a Hugging Face repository **revision** (commit
hash) with the exact file list and sizes, and the SHA-256 of `model.bin` from the Hugging Face
API. Multilingual models only (no `.en` variants), because content includes non-English speech.
Default model `small`, compute type `int8`, beam size 5, VAD on, word timestamps on, CPU
threads left to faster-whisper (`0`). All are settings (`IFTYEDITS_TRANSCRIPTION_*`).
**Benchmark:** see the table under "Benchmark results" below, produced with
`scripts/benchmark_transcription.py`. Only models that could be downloaded over the available
network were measured.
**Consequences:** The same model name always means the same weights. Changing a pinned
revision changes the transcript parameter hash, so old transcripts are not silently reused.

### Benchmark results

Measured 2026-11-21 on the M1 Max (32 GB, macOS 26.2) with faster-whisper 1.2.1 /
CTranslate2 4.9.0, beam 5, VAD and word timestamps on, each configuration in a fresh process
(`scripts/benchmark_transcription.py --models tiny base --threads 0 4 8 --repeats 6`).
Audio: 222.9 s of English speech synthesised with macOS `say` from the script's fixed text,
which is also the WER reference. Synthetic speech is much cleaner than real podcasts, so
these WER values are optimistic; speed is representative.

| Model | Device / compute | Threads | Load (s) | Transcribe (s) | Speed (x real time) | Peak RAM | WER |
|---|---|---|---|---|---|---|---|
| tiny | CPU int8 | default (4) | 0.1 | 6.7 | 33.2 | 0.74 GB | 5.5% |
| tiny | CPU int8 | 4 | 0.1 | 6.6 | 33.6 | 0.73 GB | 5.5% |
| tiny | CPU int8 | 8 | 0.1 | 10.2 | 21.8 | 0.74 GB | 5.5% |
| base | CPU int8 | default (4) | 0.2 | 11.0 | 20.3 | 0.83 GB | 4.5% |
| base | CPU int8 | 4 | 0.2 | 10.8 | 20.6 | 0.82 GB | 4.5% |
| base | CPU int8 | 8 | 0.2 | 21.2 | 10.5 | 0.85 GB | 4.5% |

Apple Metal comparison (same audio, same process, one run each):

| Model | CPU int8 | MPS float16 (experimental) |
|---|---|---|
| tiny | 6.4 s (35.0x) | 9.9 s (22.5x) |
| base | 10.2 s (21.8x) | 25.5 s (8.7x) |

Findings: 8 threads is slower than the default (CTranslate2 uses 4 intra-op threads by default;
8 contends with the efficiency cores and other work), so `IFTYEDITS_TRANSCRIPTION_CPU_THREADS`
stays `0`. MPS is slower than CPU int8 for these small models. Requesting `int8` on MPS is
silently run as float16 by CTranslate2.

**Not measured:** `small` (the default), `medium`, `large-v3-turbo` and `large-v3` could not be
downloaded in reasonable time over the network available on 2026-11-21 (often 5–50 KB/s), and
mlx-whisper was not installed (D15). `small` is the default because it is the usual
speed/accuracy balance for Whisper models; confirm with
`scripts/benchmark_transcription.py --models small` once it is downloaded, ideally with a real
recording and a hand-checked reference (`--audio`, `--reference`). Memory figures in the model
catalog for models other than `tiny` and `base` are estimates.

## D17 — Transcript storage, reuse and search (2026-11-21)

**Context:** Later phases (moment detection, captions) need structured, timestamped text;
the UI needs search; re-running must not create duplicates.
**Decision:** Migration `0004_transcripts` adds `transcripts`, `transcript_segments` and
`transcript_words`, plus an FTS5 index `transcript_search` over segment text kept in sync by
triggers (tokenizer `unicode61 remove_diacritics 2`). A transcript row stores provenance: the
audio artifact's SHA-256, engine and version, model and pinned revision, device, compute type,
requested and detected language with probability, parameters and their hash, counts and
processing time. It is written once, with all segments and words, in one transaction after a
job-ownership check; run state (progress, errors, cancellation) stays on the job. Identity is
(video, parameter hash): the same parameters reuse the stored transcript (or replace it with
`force`); other parameters add a version. Exactly one transcript per video is current
(partial unique index); later phases read `TranscriptRepository.current()`. Transcript ids use
`AUTOINCREMENT` because they appear in job results and exports. Engine output passes through
`core/transcript.normalize_segments`: finite, non-negative, millisecond-rounded times, end not
before start, nothing past the audio end (0.5 s tolerance clamped, beyond that dropped and
counted), words inside their segment, text stripped. Overlapping segments are kept. Search
quotes every user word as an FTS5 prefix term, so user input cannot inject FTS syntax.
SRT, WebVTT, TXT and JSON exports are generated from the database on demand; `--save` writes
under `data/exports/transcripts/` after `assert_safe_output_path`.
**Consequences:** No partial transcripts after failures or cancellation, and an earlier
transcript survives a failed re-run. Languages without spaces are only searchable by whole
runs (no CJK segmentation). One language per transcript; no diarisation.

## D18 — Explicit, verified model downloads (2026-11-21)

**Context:** Roadmap 1C.2 requires showing the download size and disk space before
downloading; CLAUDE.md requires model weights outside Git and clear errors for missing models.
**Decision:** Transcription loads models with `local_files_only=True` and never downloads.
Downloads happen only through a `download_model` job (Transcripts page) or
`iftyedits models download`, using `urllib` against `https://huggingface.co/<repo>/resolve/<revision>/<file>`
(any other URL is refused). The store checks free space (download size + 512 MB), streams into
a hidden `.download-*` folder under `models/faster-whisper/` with byte progress and
cancellation, verifies sizes and the weights' SHA-256, writes a manifest, and only then renames
the folder into place. Abandoned staging folders older than an hour are removed when a worker
starts. A models directory inside the originals vault is refused. `huggingface_hub` (a
faster-whisper dependency) is not used for downloads because it offers no byte-level progress
callback or cancellation.
**Consequences:** Downloads are reproducible and auditable, but cannot resume; a cancelled or
failed download starts again. Installed-model checks compare sizes (cheap); `models verify`
re-hashes the weights.


---

## D19 — Clip intelligence is deterministic, local and explainable (2026-11-22)

**Context:** Phase 2 must rank suggested Shorts without a paid API, with reasons the user can
check, and without claiming anything it did not analyse.
**Decision:** Signals, candidate generation and scoring are plain deterministic algorithms in
`core/` (no model, no network, no randomness). `numpy` is a direct dependency for the loudness
analysis. A score is a weighted sum of 12 named components minus penalties; the stored record
keeps every component value, weight and explanation, plus `SCORING_VERSION` and the
generation version, so a result can be recomputed. Confidence measures evidence quality (word
timings, transcript reliability, language support, audio and scene data present), not the
chance of success. Wording cues exist for English only; other languages skip those components
(weight 0, "Not assessed"). Faces, speakers and emotions from tone are not analysed, and the
UI says so. Results are recommendations; they are not a prediction of views and not a
copyright or policy check.
**Consequences:** Results are reproducible and testable, and work offline. Quality is bounded
by simple cues; an optional language-model provider (roadmap 2B.3) and OpenCV face detection
(2A.3) remain future, opt-in work.

## D20 — Scene changes from FFmpeg, not PySceneDetect (2026-11-22)

**Context:** Roadmap 2A.1 allows PySceneDetect "or a justified alternative".
**Decision:** Scene changes come from FFmpeg's `select='gt(scene,T)'` filter run on the 720p
proxy through the existing transcoder adapter (threshold `IFTYEDITS_CLIP_SCENE_THRESHOLD`,
default 0.3). Work files go under `data/derived/.clip_analysis_work/` after
`assert_safe_output_path()`.
**Consequences:** No new dependency and one decode of the small proxy. Cut detection is
coarser than PySceneDetect's adaptive detector. Scenes only snap clip boundaries and add
evidence, so the weaker detector is acceptable. A missing proxy or FFmpeg failure is
recorded in the analysis `sources` and the analysis continues without scenes.

## D21 — Clip storage: replaceable analyses, durable decisions (2026-11-22)

**Context:** Re-analysis must not duplicate results or lose the user's choices, and a failed
run must not leave partial results.
**Decision:** Migration `0005_clips.sql` stores analyses, signals and candidates.
`UNIQUE(video_id, params_hash)` makes a retry replace the earlier row; a partial unique index
keeps one `is_current` analysis per video. All rows of an analysis are written in one
transaction after `assert_job_owned`, so a failed or cancelled job writes nothing. Favorite,
reject and adjusted boundaries are stored apart, keyed by `(video_id, candidate_key)` with a
snapshot of the span, where the key is a hash of the video SHA-256 and the boundaries; they
survive re-analysis. The feedback fingerprint is part of the parameters hash, so a new
decision makes the next analysis re-rank instead of reusing the old one. Reuse is checked
before the expensive passes. An analysis that ran without a requested source (for example, a
damaged audio file) is marked degraded and is not reused.
**Consequences:** Re-running is cheap and idempotent. Changing a transcript, a setting or
feedback produces a new analysis automatically. Decisions about a boundary that later moves
by a different key no longer attach to the new candidate.


---

## D22 — Model downloads retry and resume; stale processes are an operational risk (2026-11-22)

**Context:** Two real `small` downloads failed (an incomplete `model.bin`, then a read timeout)
and each restarted from zero. Separately, a dashboard and worker started before the Phase 2
code landed kept the old modules in memory and failed with `ImportError` on the new job kind
and error class, although the files on disk were correct.
**Decision:** This supersedes the "cannot resume" consequence of D18. A file download now makes
up to 4 attempts. Transient failures (network errors, timeouts, dropped connections, HTTP 429
and 5xx, short bodies) keep the partial file and the next attempt sends an HTTP `Range`
request; the partial bytes are re-hashed first, so the SHA-256 check still covers the whole
file. A server that answers 200 instead of 206 makes the file restart. Client errors (404),
oversized bodies and checksum mismatches fail at once. Nothing is installed until every file
passes its size and SHA-256 check, as before. Python does not reload imported modules, so after
updating the code stop `iftyedits ui` and start it again (the guide says so).
**Consequences:** A flaky connection no longer throws away hundreds of megabytes. Resume
across separate jobs is not implemented: a failed job's staging folder is discarded.


---

## D23 — Boilerplate penalty, stricter affect labels, strengths and weaknesses kept apart (2026-11-22)

**Context:** The real GTA 6 analysis ranked a closing "like and subscribe" clip fifth and an
intro line fourth. Ordinary commentary was labelled "Funny moment" or "Emotional moment" on one
mild word, and a component's detail text such as "Weak start" could appear under Strengths.
**Decision:** `SCORING_VERSION`, `SIGNALS_VERSION` and `CANDIDATES_VERSION` are now 2, so
existing analyses are not reused and a new run replaces them.
- *Boilerplate.* English call-to-action, channel-intro and sign-off wording is matched by
  lexicon (`core/signals.py`). Each sentence gets a strength; a clip's boilerplate share is the
  word-weighted fraction of such sentences. The penalty is proportional, not positional: 0 below
  8% share, then 45 points per unit share, capped at 25. A genuine hook with one incidental
  "subscribe" stays almost untouched. A boilerplate first or last sentence also halves the
  hook, context and payoff values. Boilerplate is never a hook, question or dialogue, and the
  generator no longer proposes a topic block that is mostly boilerplate or starts or ends on a
  boilerplate sentence by choice. The matched sentences are stored under `evidence.boilerplate`.
- *Affect.* Humor or emotion is accepted only with a strong cue (weight 0.8 or more), two cues,
  an exclamation mark, or a short reaction opener. Negated cues are dropped ("not funny"), and
  literal uses such as "I guess what" are blocked. A weak single word no longer sets the
  category.
- *Strengths and weaknesses.* Strengths come only from the positive text of components scoring
  0.7 or more. Weaknesses list penalties first, then components under 0.4, otherwise the
  component's specific flaw. A component that found no cue is not reported as a weakness.
**Consequences:** Scores of genuine clips are unchanged or nearly so; intro, outro and CTA clips
drop. Limits: English only; heuristic text cues, no tone-of-voice analysis; CTA wording outside
the lexicon is missed, so a partly recognised outro can still appear low in the list; a mid-video
host handover is not boilerplate and may still rank.

## D24 — Short editing: stored edit config, Pillow caption overlay, hard-linked exports (2026-11-22)

**Context:** Phase 3 must turn a Phase 2 suggestion into a finished vertical MP4 without ever
changing the suggestion, the transcript or the original, and the installed FFmpeg has no
`subtitles`, `ass` or `drawtext` filter.
**Decision:**
- *Edit config, not edited clips.* A `short_projects` row (migration `0006`) is keyed by
  `(video_id, candidate_key)` and holds one JSON `ShortConfig` (trim, crop, captions, look,
  audio, output). The AI start/end stay in the candidate; the config stores only the user's
  values, so "Reset to the AI suggestion" is exact and switching clips can never leak settings.
  Caption text edits live in the config as per-cue overrides; the transcript is read-only.
- *Crop.* Full source height, width = height x 9/16 rounded down to an even integer (1280x720
  keeps a 404x718 window; 1920x1080 keeps 606x1078, then scales to 1080x1920), x from a 0-100
  slider clamped to the frame, y centred. Already-9:16 sources are not cropped; narrower
  portrait sources keep full width. Scaling is uniform so nothing is stretched. Preview and
  export call the same function. No face detection.
- *Captions.* Drawn with Pillow into transparent RGBA PNGs, listed in a concat-demuxer input
  and composited with `overlay`. Timing is word, segment or estimated and labelled as such;
  active-word highlight only runs with real word timing. Pillow is the one new dependency.
- *Render.* H.264 (`h264_videotoolbox` when `ffmpeg -encoders` lists it and a probe succeeds,
  else `libx264`; failure at runtime retries with libx264) plus AAC 48 kHz, yuv420p, 30 fps
  (60 for 60 fps sources). Optional `loudnorm` (single pass, -16 LUFS); volume above 100%
  adds `alimiter`. Missing audio is not an error. HDR is not tone-mapped.
- *Output.* Work in `derived/shorts/work/render_<id>` (removed after), encode to
  `exports/shorts/.partial/render_<id>.partial`, verify with ffprobe (streams, size,
  duration within tolerance) and a full `-xerror` decode, then hard-link to
  `exports/shorts/<slug>_v<id>_<start>-<end>_<hash8>[-N].mp4` with `claim_unique_path`, so an
  existing file is never replaced. Previews go to `derived/shorts/previews/`.
- *Jobs.* `RENDER_SHORT` reuses the queue and worker. A unique partial index allows one active
  render per config hash; a repeat click returns the running render. Progress comes from
  FFmpeg `-progress`. On worker start, renders whose job vanished or died (60 s grace) are
  closed as failed; `sync_renders` closes renders whose job was cancelled or failed.
**Consequences:** Edits are cheap to store and reproduce (config hash covers video SHA and
settings). Limits: single-pass loudnorm is approximate; HDR looks flat; the preview is a
540x960 render of the same graph, not a live player; one worker runs one render at a time;
no intelligent reframing, transitions or multi-clip timeline (future enhancements).

## D25 — Phase 4: URL ingestion, one-job production, detail-based framing (2026-11-22)

**Context:** Phase 4 asks for link ingestion, an automated link-to-Shorts pipeline, better
reframing and captions, and a redesigned interface.
**Decisions:**
- *yt-dlp as a subprocess.* One new dependency (`yt-dlp`, locked). It is run through
  `subprocess.run` with an argument list and timeouts by `adapters/ytdlp.py`; no cookies, no
  login, no DRM or age-gate circumvention, no playlists, channels or live streams. Only
  `youtube.com/watch`, `youtu.be` and Shorts links are accepted (`core/sources.py`). Limits:
  `IFTYEDITS_URL_MAX_DURATION_S`, `_URL_DOWNLOAD_TIMEOUT_S`, `_URL_MAX_FILESIZE_MB`. The UI and
  CLI state that a download is not permission to reuse a video. Downloads go to
  `data/staging/` and are imported through the same vault path as local files
  (`IngestService.import_download`); the link is recorded in `video_urls` (migration `0007`)
  so a repeat link maps to the stored video and is never downloaded twice.
- *One production job.* `produce_video` runs fetch, prepare, transcribe, analyse, then renders
  each chosen clip one after another inside the same job. Every stage is idempotent and reuses
  stored output; a failed clip is recorded and the others still export; cancel is checked
  between steps. Renders are serialised on purpose (memory and encoder load on one Mac).
  Exports go to `exports/shorts/<slug>-v<id>/` and existing files are never replaced.
- *Clip scoring unchanged.* The Phase 2 scorer is already componentised (12 explained
  components, `SCORING_VERSION = 2`, boundary and boilerplate logic with deterministic tests),
  so no rewrite or version bump was made.
- *Auto-framing without a model.* OpenCV 5.1 was tried and rejected: its wheel ships no Haar
  cascades or models, so no face detector runs without a separate download. Framing instead
  measures edge energy per column on a few sampled frames and picks one steady crop position
  per clip (`core/framing.py`); flat or ambiguous footage keeps the centre. It is a detail
  heuristic, not face or speaker tracking. A crop the user set by hand is never overridden, and
  any failure falls back to the centre.
- *Captions.* Two new presets (`impact`, `studio`) with safe-area tests; emphasis words are not
  implemented.
- *Interface.* Native Streamlit dark theme (`.streamlit/config.toml`) plus a small CSS and HTML
  helper (`ui/theme.py`, no I/O). Onboarding choices (dismissed welcome) are a JSON file in the
  data directory (`services/preferences.py`), not a table.
**Consequences:** Link ingestion depends on yt-dlp keeping pace with YouTube; update it with
`uv lock --upgrade-package yt-dlp`. Framing cannot follow a moving subject within a clip.

## D26 — Phase 4B: density scoring, moving crop, caption emphasis, selection policy (2026-11-22)

**Decision.** Supersedes the framing and caption limits stated in D25.
- *Scoring v3.* A `density` component (informative words per second, boilerplate excluded) joins
  the existing hook, completeness, context, payoff, pacing, clarity and engagement components.
  `SCORING_VERSION` is 3, so stored analyses re-run when the user asks. Strengths, weaknesses and a
  plain reason are kept per candidate; scores are explanations, not predictions.
- *Selection policy.* Production picks by score, favours the user's favourites, skips rejected
  clips, near-duplicates (overlap or less than `min_gap_s` apart) and lengths outside
  `min_duration_s`..`max_duration_s`, and never pads to reach the count. `--review` (or the
  "Review the picks" box) selects and reports without rendering.
- *Moving crop without a face model.* Evaluated: `cv2` is not installed; `onnxruntime` and numpy
  arrive as transitive dependencies but no face model ships with them, and a detector's output
  layout cannot be verified offline without the model and a face image. No face detector is
  therefore shipped. Instead `core/framing.follow_subject` turns per-sample detail readings into a
  smoothed path (median of 3, dead zone 0.08, speed limit 0.12 per second, instant jump only at
  a detected scene cut, steady fallback when travel is under 0.1, none when readings are weak).
  The path is stored as `ShortConfig.crop_path` (at most 32 keyframes) and rendered by an FFmpeg
  `crop` filter whose `x` is a piecewise-linear expression in `t`; a frame at 5.5 s matched a
  static crop at the end position. It follows the busiest part of the picture, not faces or
  speakers. No identity or embedding is stored. A manual crop value or an edited trim drops the
  path, and production never replaces a non-default crop.
- *Caption emphasis.* A deterministic rule (`core/captions.emphasis_indexes`) marks at most a few
  long, numeric, all-caps or exclaimed content words per cue; stop words are never marked.
  Presets `clean`, `punchy`, `cinematic` and `mobile` were added. Config hashes of existing Shorts
  are unchanged because the new keys are omitted when unused.
- *URL ingestion tested for real* on a Creative Commons video (Blender's Big Buck Bunny): metadata
  and a 135 MB download through `YtDlpDownloader` worked without credentials.

**Consequences.** Without a detector the crop cannot keep a person in frame; adding one later
means a new reading source for `follow_subject`, not a new render path.

## D27 — Phase 4C: failed production is failed, replies do not open clips (2026-11-22)

**Decision.** A production job whose rendered clips all failed ends `failed` with the clip
reasons; any success keeps the job succeeded and the summary reports the failures. Candidate
starts avoid short reply lines ("No way.") when an earlier setup line is available, and
`CANDIDATES_VERSION` is 3.

**Why.** The job status is what the owner reads first, so success with zero Shorts misleads.
A clip that starts on an answer to a question it omits is not understandable alone.

**Trade-offs.** The reply rule is a word-list heuristic (four words or fewer, English openers).
Existing analyses are not rewritten; the version bump changes the parameter hash, so the next analysis stores a new one beside the old.

## D28 — Phase 4C: cancel watcher and framing dwell (2026-11-22)

**Decision.** yt-dlp downloads are stopped by a watcher thread that polls the cancel flag, not
by a check on each output line. The moving crop starts to move only after two consecutive
readings on the same side of the dead zone (`DWELL_READINGS`), and ties between equally good
windows resolve to the middle of the tied run.

**Why.** A stalled download prints nothing, so a per-line check never saw Cancel. Two people
talking in turn produced alternating readings and the window swung between them.

**Trade-offs.** The window reacts about one sample (about two seconds) later. Fast lateral
action still outruns the pan; this is a limit of a detail heuristic, not a bug to hide.

## D29 — Phase 4C accepted with a documented real-media gap (2026-11-22)

**Decision.** Phase 4C is accepted for development without real podcast, sports, slides, noisy or
multilingual footage. Real-media validation is mandatory before any release (Phase 9).

**Why.** Downloads need per-file permission, and finishing the application matters more than
repeating the same milestone. All automated gates pass.

**Trade-offs.** Entertainment quality and real-world multilingual performance are unvalidated.

## D30 — Phase 5 UI: group views instead of rewriting them (2026-11-22)

**Decision.** Navigation has eight sections. Sections that combine views (My Videos, Clip Editor)
run the existing view scripts with `runpy` through `ui/chrome.py`. An embed flag, reset by
`Home.py` on every rerun, turns page titles into section headings, and `chrome.halt()` replaces
`st.stop()` so one empty view cannot hide its neighbours. No UI library was added.

**Why.** It keeps every working page and its tests, and removes the nine-entry sidebar.

**Trade-offs.** `chrome.py` imports Streamlit, a deliberate exception to "ui has no I/O". Views
that run in a section share one session, so widget keys must stay unique across them.

## D31 — Phase 6: a provider-neutral text model, off by default (2026-11-22)

**Decision.** AI writing and review use a small `TextModel` protocol with one adapter for any
OpenAI-compatible chat endpoint (standard library `urllib`, no SDK). The default endpoint is local
Ollama, and AI stays off until `IFTYEDITS_LLM_MODEL` is set. With no model the studio says so and
offers manual writing and a manual review; it never fills fields with canned text.

**Why.** No provider existed, local-first processing is a project priority, and a hosted SDK would
add a dependency and send private transcripts out by default.

**Trade-offs.** The owner must run or point to a model. Output quality is model-dependent, so every
reply is validated and labelled as AI output.

## D32 — Readiness is derived, confirmation is fingerprinted (2026-11-22)

**Decision.** Readiness is a pure function of saved facts. Only the owner's checklist ticks,
choices and final confirmation are stored. A confirmation stores the export id and a SHA-256
fingerprint of export, clip, metadata, rights and choices; a mismatch withdraws it. Risk levels are
set by fixed rules from validated flags, never by the model. Reviews and flag resolutions are
append-only. Rights belong to the source video and keep an audit trail.

**Why.** Stale approvals are the main way a gate becomes misleading. Deriving state avoids drift,
and append-only history means a concern is never silently cleared.

**Trade-offs.** Any edit after confirming means confirming again. "Ready" is deliberately weaker
than "cleared": see docs/PUBLISHING.md.

## D33 — Phase 6.10: Claude through the existing text model, standard library client (2026-11-22)

**Decision.** Claude is a second `TextModel` adapter (`adapters/claude.py`) that calls the
Messages API with `urllib`; no Anthropic SDK dependency was added. A small router
(`adapters/ai_router.py`) is the only place that picks a provider. Modes: `ollama` (default, no
external AI call), `claude` (no fallback) and `hybrid` (Claude first, local fallback; choosing
hybrid is the explicit opt-in). A fallback is always visible: the stored model reads
`ollama:<model>` and the result carries a notice. Usage (tokens, provider, model, feature, time)
goes to `ai_usage` (migration 0009); choices go to `ai_settings`. Neither holds secrets or prompt
text. The key is read only from `ANTHROPIC_API_KEY`. Limits are per calendar month (UTC), count
only Claude, and a hard limit blocks before the request is made. A USD limit with no configured
prices cannot be enforced and fails closed when hard.

**Why.** One blocking request at a time against one endpoint does not need an SDK and its HTTP
stack. Keeping the router thin avoids a second provider framework and leaves Phase 6 services
unchanged apart from labelling.

**Trade-offs.** Retry and error handling are written by hand and verified only against a local
stub of the wire format, not the live API. Prices are user-supplied, so cost is an estimate or
"unknown", never an invoice. A bad Claude answer (invalid JSON, truncation) is not retried on the
local model, so quality differences stay visible.
