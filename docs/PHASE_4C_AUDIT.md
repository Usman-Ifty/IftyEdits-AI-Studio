# Phase 4C — Production QA audit (2026-11-22)

Scope: check the Phase 4A/4B pipeline for false success, weak boundaries, UI defects and gaps in
test evidence. Baseline before this phase: 770 passed, 1 skipped (FFmpeg enabled), as reported by
Phase 4B and re-run at the start of 4C.

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | P1 | A production job reported success when every rendered clip failed. | Fixed, regression test |
| 2 | P2 | A clip could open on a bare reply ("No way.", "Wait, really?") whose setup line was left out. | Fixed, regression tests |
| 3 | P2 | Editor "AI suggestion" time range was cut off with an ellipsis at phone width. | Fixed, checked in the browser |
| 4 | P3 | No written media test matrix. | Added below |
| 5 | P3 | No evidence run on speech other than the gaming and animation sources. | Two synthetic spoken fixtures run end to end |
| 6 | Done | Production-specific cancel, subprocess kill, disk-space, concurrency, partial-failure and recovery tests were missing. | 12 production tests, 3 render tests, 2 yt-dlp subprocess tests added |
| 7 | Done | Reframing and caption matrices were missing. | Added (`test_reframing_matrix.py`, `test_caption_matrix.py`) |
| 9 | P2 | A stalled yt-dlp download ignored Cancel until the next output line or the hour-long timeout. | Fixed (cancel watcher), regression test |
| 10 | P2 | Two speakers talking alternately made the moving crop swing back and forth. | Fixed (`DWELL_READINGS`), regression test |
| 11 | P3 | A small subject was placed on the window edge because equally good windows tied. | Fixed (middle of the tied run), regression test |
| 12 | P3 | Library SHA-256 and vault path ran past the screen edge at 390 px. | Fixed (`wrap_lines`), checked in the browser |
| 13 | Open | Fast sideways action (a ball crossing the frame in about 7 s) outruns the calm pan and can leave the 9:16 window. | Documented limitation, see Reframing |
| 14 | Accepted limit | No real podcast, sports, slides, noisy-recording or multilingual footage was available as permitted media. | Accepted by the owner with this gap documented. Mandatory before any release |
| 8 | Open | Face and speaker tracking do not exist. Framing follows the busiest part of the picture. | Documented limitation |

## Fixes

1. `ProductionReport.failure_message()` returns a message when clips were rendered and none
   succeeded. `Worker._produce` raises `IftyEditsError` with it, so the job ends `failed` with the
   per-clip reasons. Partial success (at least one clip) and "no eligible clip" stay successful
   jobs with an accurate summary. Test: `test_production_where_every_clip_fails_is_a_failed_job`.
2. `core/signals.is_dangling_response` marks sentences of at most four words that open with a
   reply word. `_Timeline.start_quality` subtracts 0.4 for them, and `_start_choice` no longer
   takes the "a hook is the opening line" shortcut when the hook is such a reply, so the clip
   starts at the earlier setup line. `CANDIDATES_VERSION` went from 2 to 3. Existing analyses are
   kept. The version is part of the analysis parameter hash, so the next analysis of a video is
   not reused: it stores a new analysis next to the old one. Nothing is deleted.
   Tests: `test_a_clip_never_opens_on_a_short_reply_to_an_earlier_line`,
   `test_dangling_response_detection`.
3. `app/views/editor.py`: the range uses bold text plus the range instead of `st.metric`.

4. `adapters/ytdlp.py`: a watcher thread polls the cancel flag every 0.2 s and kills the
   process, so a silent, stalled download stops at once. Test (real subprocess, a script that
   hangs): cancel returns in under 10 s and the process is gone; a stall is killed at the timeout
   with a plain message that holds no path or traceback.
5. `core/framing.follow_subject`: the window moves only after `DWELL_READINGS` (2) readings on
   the same side of the dead zone. A lone or flip-flopping reading no longer moves it.
6. `core/framing.best_window`: among equally good windows, the middle of the tied run nearest the
   centre is used.
7. `app/views/library.py`: the integrity block wraps long lines.

Limit: this is a short-word heuristic, not semantic understanding. A long reply, or a reply in
another language, is not detected.

## Production reliability tests (all automated, none touch `data/`)

| Area | Evidence |
|---|---|
| Cancel during production | Job ends `cancelled`, finished Shorts kept, no half file |
| Subprocess termination | ffmpeg: SIGTERM then kill, verified by pid (existing tests). yt-dlp: real subprocess, killed on cancel and on timeout, verified by pid |
| Disk space | Below `min_free_bytes`: fails before encoding with a readable message. Disk full mid-encode: no `.partial`, no export row, readable error |
| Concurrency | Several connections claim one job atomically; two workers never run the same job |
| Partial failure | Some clips fail: job succeeds, summary lists failures. All fail: job failed |
| Interrupted processing | Stale running jobs are requeued up to `max_attempts`, then failed; an already-finished export is reused, never replaced |
| Repeated export | Same config reuses the finished file |

Not covered: two real worker processes rendering at once on real media (the claim is tested at
database level), and a real yt-dlp network failure.

## Matrices

- Captions: 8 transcripts (short, long, rapid dialogue, silence, long word, Unicode, missing
  word times, clip boundary) times all 10 presets. Checks: ordered, non-overlapping, inside the
  clip, never a flash, nothing lingers past six seconds, nothing dropped or invented, safe
  area, contrast of at least 7:1 for text and 4.5:1 for emphasis.
- Reframing: scenarios (single subject, two alternating speakers, cut, edge motion) and a crop
  size matrix over landscape, square, portrait and odd-sized sources. Checks: even dimensions,
  inside the frame, 9:16 within rounding, travel and speed limits.

## Media validation

Reproduce with a scratch library: `IFTYEDITS_DATA_DIR=<scratch> uv run iftyedits ...` (never the
real `data/`). `tiny` model, CPU int8.

| Content | Source | Result |
|---|---|---|
| Two-voice conversation (speech synthesis) | Generated locally with macOS `say` | 35 s, 1 Short, decode clean. The first run opened on "No way."; fixed (finding 2). |
| Same conversation with pink noise mixed in (about 41 s) | Generated locally | Language detected as English (99%). Word error rate 3.1% against the script. 1 Short, 1080x1920, decode clean. |
| Spanish narration | Generated locally (`say`, voice Paulina) | Detected Spanish (100%). Word error rate 10.0% ("Hoy" heard as "o"). No clip reached score 40, so no Short; the job says so and is succeeded. |
| German narration | Generated locally (`say`, voice Anna) | Detected German (100%). Word error rate 1.7%. No clip reached score 40, so no Short, same message. |
| Slides-like lecture | Generated locally | 1 Short 25.07 s video / 25.11 s audio, decode clean. |
| Sports-like motion (a white block crossing a field, noisy audio) | Generated locally | 1 Short, decode clean. The window followed the block only partly: see Reframing. |
| Gaming footage | GTA 6 trailer footage in the owner's library (4B) | Two Shorts validated in 4B. Not re-rendered in 4C. |
| Animation, AV1 1080p, 634 s | Blender Big Buck Bunny, CC-BY (4B) | Download and probe worked. 4C ran the full job on it (about 61 s): the film has almost no speech (language probability 0.26), the analysis found no suggestions, and the job said so with no Short made. No clip was invented. |

Checks on every export: ffprobe (h264 yuv420p 1080x1920 30 fps, AAC), audio and video lengths
within 0.05 s, full decode with no errors, frames pulled at three or more times. Captions sit in
the lower middle, inside the mobile safe area, are readable, and match the speech at the sampled
times (for example "Okay, so here is the thing nobody tells you" at 1.5 s).

Not tested, no claim made: real podcast, real talking head, real sports broadcast, real slide
recordings, real noisy recordings, real multilingual speakers. Synthetic speech proves the
plumbing, the boundary logic and language detection, not entertainment quality or accent
robustness. **This gap is accepted for development and is a mandatory pre-release requirement.**

### Reframing

The moving crop is a calm pan of at most 12 % of the travel per second, steered by image detail
sampled about every two seconds. It is not face, person or ball tracking. A block that crosses
the frame in about seven seconds was lost for several samples. For fast lateral action the owner
must set the crop in the Short editor.

## Performance (M1 Max, 32 GB, `tiny`, short fixtures)

- Transcription: 22x to 34x real time on 25 to 42 s of audio (0.7 to 1.6 s).
- Whole production job (prepare, transcribe, analyse, render one Short): 2.4 to 8.0 s for 25 to
  42 s sources. The render of a 14 s Short takes about 2 s.
- Larger models and long sources were benchmarked in 1C and not repeated here.

## Log and sensitive-data review

`logging` calls were read. Job, stage and import messages carry ids, file names and the first 12
characters of a hash. No transcript text, URL query string, cookie, token or credential is
logged. yt-dlp runs with `--ignore-config`, no cookies. ffmpeg error text has paths redacted.
Local paths appear in the "vault file does not match its hash" error and in CLI output, which
stay on this computer. The new yt-dlp timeout error contains no path.

## Cancellation, cleanup and error handling review

Every long step takes a cancel token. External processes get SIGTERM, then SIGKILL. Renders write
to `.partial`, a work directory that is discarded, then a unique final name. Disk space is
checked first. Job errors are short plain messages; tracebacks go to the log only.

## UI audit

Isolated library, 390 px viewport, built-in browser. Import, Library, Transcripts, Exports, Jobs,
Getting started, Overview, Clips and Short editor were all inspected. `scrollWidth` equals the
viewport on every page and no element is clipped. Defects: finding 3 (earlier) and finding 12.
The Library and Jobs tables scroll sideways inside their own frame, which is the intended
Streamlit behaviour.

## Gates (2026-11-22)

`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` clean (131 source files).
`IFTYEDITS_REQUIRE_FFMPEG=1 uv run pytest`: 1015 passed, 1 skipped (opt-in real-model test).

## Acceptance

Accepted on 2026-11-22 by the owner for development, with one documented gap.

Entertainment quality and real-world multilingual performance are not yet fully validated. All
speech tests so far used synthetic voices (Spanish and German included) and generated video;
no real podcast, sports, slides, noisy-recording or multilingual footage has been run. Clip
suggestions are scored by explainable signals, not by how entertaining a clip is.

**Pre-release requirement (mandatory, part of Phase 9).** Before any release, run the pipeline on
permitted real media (podcast, sports, slides, noisy recording, and at least one non-English
language), review transcript accuracy, clip choice, boundaries, reframing, caption timing and
export validity by eye and ear, and record the result in this file. Each download needs the
owner's permission.
