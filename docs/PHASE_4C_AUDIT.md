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
| 6 | Open | Cancel-kills-subprocess, disk-space and concurrent-production behaviour have generic tests only, not production-specific ones. | Not changed, documented |
| 7 | Open | Reframing on synthetic matrix (two subjects, edge motion, portrait source) is not tested beyond the 4B unit tests. | Not changed, documented |
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

Limit: this is a short-word heuristic, not semantic understanding. A long reply, or a reply in
another language, is not detected.

## Media test matrix

Reproduce with a scratch library: `IFTYEDITS_DATA_DIR=<scratch> uv run iftyedits ...` (never the
real `data/`).

| Content | Source | Result |
|---|---|---|
| Two-voice conversation, speech synthesis, `tiny` model | Generated locally (macOS `say`), no licence issue | Full pipeline ran. One Short, 1080x1920 H.264 + AAC, 16.8 s, decode clean. The first run opened on "No way."; fixed by finding 2. |
| Single-voice narrated lecture, speech synthesis | Generated locally | One Short, 1080x1920 H.264 + AAC, 25.07 s video / 25.11 s audio, decode clean. |
| Gaming footage | GTA 6 trailer footage already in the owner's library (4B) | Two Shorts validated in 4B. Not re-rendered in 4C. |
| Animation, AV1 1080p, 634 s | Blender Big Buck Bunny, CC-BY, downloaded via yt-dlp in 4B, no credentials | Download and probe worked. |
| Real podcast, talking head, sports, slides, noisy audio, multilingual | Not available as permitted media | Not tested. No claim made. |

Synthetic speech is not a substitute for real conversation. It proves the plumbing and boundary
logic, not entertainment quality.

## UI audit

Isolated library, 390 px viewport, built-in browser: Overview, Clips and Short editor were
inspected. Layout stacks without horizontal scroll (`scrollWidth` equals `clientWidth` on the
editor). The one defect found was finding 3. Import, Library, Transcripts, Exports, Jobs and
Getting started were not re-inspected in 4C; they were reviewed in 4A and 4B.

## Not done in 4C

Production-specific cancel/subprocess tests, disk-space simulation, concurrency tests, the full
reframing and caption matrix, performance measurements and a log-hygiene review. They remain open
and are listed in `docs/PROGRESS.md`.
