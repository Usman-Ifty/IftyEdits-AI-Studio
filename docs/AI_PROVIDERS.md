# AI providers

The studio can write titles, descriptions, tags and the advisory content review with a model.
This is optional: without one you write everything yourself.

## Choose a provider (Settings → AI writing and review)

| Mode | What happens |
|---|---|
| Local model only (default) | Only your local model (Ollama) is used. No request goes to Anthropic. |
| Claude only | Only Claude is used. If it fails you get the error; nothing else is tried. |
| Hybrid | Claude first. If Claude cannot answer (no key, outage, rate limit, limit reached), the local model is used and the result says so. Choosing Hybrid is your permission for this. |

Every generated result and review shows the provider and model that wrote it, for example
`claude:claude-sonnet-5-5` or `ollama:llama3`. The choice is saved in the local database and
survives restarts. If nothing is configured the studio says what is missing and keeps your data.

## Set up Claude

1. Create an API key at console.anthropic.com and add API billing. A Claude chat subscription is
   not API access.
2. Put the key in `.env` (never in Git): `ANTHROPIC_API_KEY=...`. Do not paste it into chat or the
   terminal output. The key is never shown in the app, logged or stored in SQLite.
3. Optionally set `IFTYEDITS_CLAUDE_MODEL` (default `claude-sonnet-5-5`).
4. Restart the app, open Settings → AI writing and review, choose Claude or Hybrid, and save.

Privacy: Claude is an online service. Clip transcripts are sent to Anthropic when you generate
metadata or run a review. Use Local mode for anything private.

## Set up the local model (Ollama)

Install Ollama yourself and pull a model; the studio never installs or downloads one. Then set
`IFTYEDITS_LLM_MODEL` (and `IFTYEDITS_LLM_BASE_URL` if it is not the default
`http://127.0.0.1:11434/v1`) in `.env` and restart. "Check local connection" in Settings tests
that the endpoint answers; it sends no prompt.

## Status vs. test (Settings)

Four states are kept apart: key/model **configured**, endpoint **reachable** ("Check local
connection", also lists installed models), a **live answer** ("Test Claude" / "Test local model",
one tiny synthetic request, no project data; the Claude test uses a few real tokens), and
**unavailable** (the reason is shown). Pills say "not tested" until you run a test.

## Usage and limits

Settings shows this month's requests and tokens per provider, an estimated Claude cost, and the
latest requests (no prompt text is stored).

- Token counts are what the provider reported. A missing count stays unknown.
- Cost is estimated only when you set `IFTYEDITS_CLAUDE_PRICE_INPUT_PER_MTOK` and
  `IFTYEDITS_CLAUDE_PRICE_OUTPUT_PER_MTOK`; otherwise it shows "unknown". Prices are never guessed.
- Monthly USD budget and monthly token limit (0 = none). A warning appears at 80%. With "Stop Claude
  requests when the limit is reached" on, further Claude calls are blocked before they are sent.
  A USD limit without prices cannot be checked; with the hard stop on, Claude stays blocked until
  prices are set or the limit is removed. A token limit works without prices.
- "Largest reply per request" caps the output tokens asked for.
- Local model requests are recorded but are never counted as an Anthropic charge.

## Limitations

- The Claude client was tested against a local stub of the Messages API and was not run against
  the live service in this project.
- Usage counts only requests made by this studio, on this computer; it is not your Anthropic
  invoice. Check console.anthropic.com for the authoritative bill.
- Calendar months are UTC.
- A Claude answer that is invalid or cut off is reported, not retried on the local model.
