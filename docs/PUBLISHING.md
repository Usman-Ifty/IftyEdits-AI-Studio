# Upload preparation (Phase 6)

Everything in the **Upload package** section of the Clip Editor. Nothing here uploads, schedules,
publishes or contacts YouTube. The studio prepares a package; the owner copies it into YouTube.

## What "ready" means

"Ready for upload (you confirmed)" means the owner finished the checklist and confirmed. It does
**not** mean the clip is copyright-cleared, fair use, policy-compliant, monetisable or safe. AI
review is advisory and can miss problems or raise false ones.

## AI provider

Providers (local Ollama, Claude, hybrid), limits and privacy are documented in
[AI_PROVIDERS.md](AI_PROVIDERS.md). The text below describes the local endpoint.

No model ships with the studio. Any OpenAI-compatible `/chat/completions` endpoint works
(`adapters/llm.py`, standard library only). The default endpoint is local Ollama
(`http://127.0.0.1:11434/v1`). AI is **unconfigured until `IFTYEDITS_LLM_MODEL` is set**; then
metadata generation and content review report "unavailable" and the owner writes the fields and
records a manual review. There is no template fallback pretending to be AI.

| Variable | Default | Purpose |
|---|---|---|
| `IFTYEDITS_LLM_MODEL` | empty | Model name; empty means AI is off |
| `IFTYEDITS_LLM_BASE_URL` | `http://127.0.0.1:11434/v1` | Endpoint |
| `IFTYEDITS_LLM_API_KEY` | empty | Optional bearer key (hidden, never logged) |
| `IFTYEDITS_LLM_TIMEOUT_S` | `180` | Per-request limit |
| `IFTYEDITS_LLM_MAX_TRANSCRIPT_CHARS` | `12000` | Transcript text sent per request |

A non-local endpoint receives clip transcripts; Settings says so.

## Metadata (6A, 6B)

Per Short, stored in `publish_metadata` with a per-field marker of who wrote it (AI or you):
3 to 5 title options with a style and a recommended one, short and long description, hashtags and
search tags (kept separate, deduplicated), topic, category, audience, language, confidence, pinned
comment, hooks, call to action, summary, search phrases, thumbnail text/concept/composition/frame,
and upload advice. Rules:

- Grounded in the clip transcript (timed lines in clip time, with your caption edits applied).
  The transcript is sent as data with an instruction not to follow text inside it.
- Output is parsed and validated; malformed, empty or wrongly typed replies save nothing. URLs the
  model invents are removed (only the saved source URL is allowed). Thumbnail times outside the
  clip are dropped.
- Whole or per-group regeneration. A title you chose is never overwritten.
- Editing a field marks it as yours and withdraws its "reviewed" tick.
- Title limit 100 characters (warning above 70); warnings for all caps, repeated punctuation and
  hype wording. Tags capped at 500 characters in total. Hashtags need a letter.
- A changed clip (range or caption text) marks metadata stale until regenerated or accepted.
- Thumbnail: a real frame from the source via the existing still extractor. No fake image.

## Content review (6C)

Categories: harassment, hate, threats, violence, dangerous acts, self-harm, sexual content, minors,
misinformation, scams, drugs, sensitive events, privacy, deceptive editing, other.

- Each flag has a category, severity, timestamps, an excerpt and an explanation. A flag counts as
  evidence only if its excerpt is found in the transcript; otherwise it is "inferred" and "high"
  becomes "medium".
- The risk level comes from fixed rules, not from the model: **Low detected risk**, **Review
  recommended**, **High risk (manual review required)**, **Unable to assess**. "Low detected risk"
  never means safe.
- No model, no transcript or a failed call gives "Unable to assess". The owner may record a manual
  review (note required), which satisfies the checklist item.
- Reviews are append-only. A flag can be dismissed as a false positive (note required) or
  acknowledged as still a concern; both are kept in history, survive re-review, and can be
  reopened. Dismissing never deletes the flag. Editing the clip marks the review stale.

## Rights (6D)

One record per **source video**; every Short cut from it inherits it. Fields: source title,
creator, URL, source ID, owner, license name and link, evidence, permission notes, credit
required, commercial and derivative flags, expiry date, reviewed date and the owner's
confirmation. Statuses: original, permission obtained, verified license, Creative Commons
(verification required), public domain (verification required), requested, unknown, restricted,
review required.

- Nothing is auto-assigned. Fields are pre-filled from the download only as unconfirmed hints, and
  a license label reported by a site is shown as a reminder, never applied.
- Statuses that claim permission need recorded evidence. A change to the record writes an audit
  entry (old and new status, fields changed, time).
- Credit lines are generated only from fields you entered. No link or attribution is invented.

## Readiness (6E)

Computed from saved data by `core/readiness.py` (pure) and persisted in `publish_readiness`.

Status order (first match wins): **Blocked**, **Draft**, **Metadata incomplete**, **Rights
unverified**, **Review required**, then "Checklist done, awaiting your confirmation", then **Ready
for upload (you confirmed)**.

Blockers (a tick cannot clear them): rights status `restricted`; an unresolved high-severity flag
(dismiss it with a note if it is a false positive).

Required items: metadata written and current; valid title and description; title, description,
hashtags and search tags reviewed; clip watched; transcript checked; language checked; disclosures
considered; final exported video watched; a content review done on the current clip (automated
success or manual; "unavailable" and "failed" do not count); every flag resolved or acknowledged;
rights reviewed and confirmed by you, with evidence where the status needs it and a credit line
where credit is required; audience (made for kids or not) and visibility chosen by you; a finished
export that exists in the exports folder, matches its recorded size and matches the current clip
settings. Unverified rights (unknown, requested, review required, Creative Commons, public
domain) additionally need an explicit acknowledgement, which is shown as a caveat and never
clears the source.

**Confirmation** is a separate explicit click, stored with the export id and a fingerprint of the
export, clip text/range, metadata, rights, upload choices and acknowledgement. Any change to those
withdraws it (an event is logged) and the status falls back. The owner can also withdraw it.

## Package export

JSON or TXT, written under `data/exports/metadata/` with an exclusive-create unique name (never
overwrites; path checked with `assert_safe_output_path`). A JSON download is also offered. The
package includes the readiness status and open items, the disclaimer, title options, the
description as it would be pasted, hashtags, tags, thumbnail plan, upload choices, source and
rights summary, review summary with flags and your resolutions, and which fields AI wrote.

## Storage (migration 0008, additive)

`publish_metadata`, `safety_reviews` (append-only), `safety_resolutions` (append-only),
`source_rights`, `source_rights_history`, `publish_readiness`, `publish_events`. No existing table
is altered.

## Limits

- Review and metadata quality depend on the chosen model and the transcript; both can be wrong.
- Reviews read the transcript only; they cannot see video, hear audio or detect music rights.
- The studio cannot verify licenses or ownership. It records what the owner states.
- Only the English prompts have been exercised in tests; non-English clips rely on the model.
- No real model was run in the test suite (a fake `TextModel` is used). Quality of real output is
  unmeasured until a model is configured.
