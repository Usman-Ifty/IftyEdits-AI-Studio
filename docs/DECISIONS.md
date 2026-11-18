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
