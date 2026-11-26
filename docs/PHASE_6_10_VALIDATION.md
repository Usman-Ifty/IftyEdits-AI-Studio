# Phase 6.10 validation: hybrid AI providers

## What was verified (mocked, no paid calls)

- Claude adapter against a local stub HTTP server using the Messages wire format: request shape
  and headers, token extraction, missing counts stay unknown, 401/403/404/429/5xx/529/400 mapping,
  no retry on auth errors, bounded retry honouring `retry-after`, refused connection, timeout,
  malformed and truncated replies, missing key, non-http endpoint, key never in errors.
- Router: Claude-only never falls back; Local-only never calls Claude; Hybrid falls back visibly
  (`ollama:<model>` plus a notice); no fallback on a bad Claude answer; missing key / missing
  local model give actionable errors; output-token cap.
- Budgets: warning at 80%, hard block before the call, soft limit only warns, USD limit without
  prices is unenforceable and fails closed when hard, local usage never counted, other months
  ignored, hybrid falls back when the hard limit blocks.
- Persistence: choices and usage survive reopening the database; no key in SQLite or `repr`.
- Phase 6 flows through the router: generate all, regenerate one group, manual fields and
  `manual`/`ai` markers preserved, failure changes nothing, review labelled with provider, failed
  review never counts as passed, package shows the provider. Existing 6.9 tests still pass.
- Settings page (AppTest): shows setup help without a key, saves the mode, never renders the key.

Gates: Ruff, format, mypy clean; 1196 passed, 1 skipped.

## What was NOT verified

- **Real Claude API**: no `ANTHROPIC_API_KEY` is set (checked without printing it), so no live
  call was made. Real response shapes, real rate-limit behaviour and real token counts are
  unconfirmed. No transcript was sent anywhere.
- **Real Ollama**: not installed, port 11434 unreachable. Not installed or downloaded by this work.
- Output quality of either model on real clips.
- Browser/visual check of the new Settings tab at 390px was not re-run after this change.

## How to validate for real

1. Add `ANTHROPIC_API_KEY` to `.env` yourself (not in chat). Restart.
2. Settings → AI writing and review → Claude only → Save.
3. Open one short, non-sensitive clip → Upload package → Generate all. Transcript text is sent to
   Anthropic when you do this.
4. Check the pill shows `AI: claude:<model>` and Settings shows tokens for the month.
5. Optional: set a tiny token limit with the hard stop on and confirm the next call is blocked.
