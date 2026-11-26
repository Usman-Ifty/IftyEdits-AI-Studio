# UI audit (2026-11-22)

Run in the built-in browser against `uv run iftyedits ui --port 8612` with an isolated, empty data
directory (the real `data/` was not used).

| Check | Result |
|---|---|
| All 8 pages load at 375px | Yes: Dashboard, Create Shorts, My Videos, Clip Editor, Finished Shorts, Processing Queue, Settings, Guides & Help. No exception element, no horizontal overflow (page width 371 of 375). |
| Settings → AI tab text | Rendered: provider choices, Claude "Not configured" with setup help, Local "Not configured", limits, usage. |
| Select Hybrid and Save | Persisted to SQLite (`ai_settings.mode = hybrid`); after a full reload the Hybrid radio was selected. |
| Check local connection | Clicked; result text was not re-read after the click, so its wording in the browser was **not verified** (covered by unit tests). |
| Key never shown | Unit test; the key was never configured in the browser run. |

Not tested in the browser: tablet width, Claude Only / budget / hard-stop interaction, the publishing
flow (Upload package, Generate all, rights, readiness) with a real project, file upload, dropdowns.
Those are covered by AppTest and service tests only. Visual dark-theme review was limited to a small
screenshot of the Settings tab. Usage tables are canvas-rendered, so their text was not read in the browser.
