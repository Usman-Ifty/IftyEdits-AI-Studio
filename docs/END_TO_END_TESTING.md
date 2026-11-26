# End-to-end testing (2026-11-22)

| Area | Evidence |
|---|---|
| Automated suite | See docs/TESTING.md and the final audit report for counts. |
| Import → prepare → transcribe → analyse → render | Real run on a 906 s local video: `iftyedits import`, then `iftyedits produce 824f2065 --clips 1 --min-length 20 --max-length 45`; job 1 `succeeded` ("1 of 1 Shorts exported"). |
| Hybrid / provider routing | Mocked: stub Messages server and fake models (auth failure, 429, timeout, truncation, malformed, Ollama fallback, both unavailable, budgets). |
| Live Claude | Not run: no key. |
| Live Ollama | Not run: not installed. |
| Phase 6 publish flow in a browser | Not run; AppTest and service tests only. |
