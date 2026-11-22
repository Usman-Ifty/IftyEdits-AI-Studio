# Optional YouTube upload (Phase 7)

Source of scope: `docs/ROADMAP.md`, "Phase 7 — Optional YouTube Integration" and its acceptance
criteria. Everything here is optional. Without setup, the **Package** tab (copy/paste or save a
JSON/TXT package) works exactly as in Phase 6.

## What it does

From **Clip Editor → Upload package → YouTube**, after the Phase 6 checklist is confirmed:

1. **Review upload details** shows exactly what will be sent: destination channel (read from YouTube),
   the export file and its SHA-256, title, description, search tags, visibility and the made-for-kids
   answer you gave. Nothing is sent yet.
2. You type `I approve this upload` and press **Approve and upload**. The upload runs in the background
   worker as **Private or Unlisted only**.
3. When it is done you get a link to YouTube Studio. The studio never publishes or schedules; you do
   that in YouTube Studio.

Safety rules enforced in code (`core/youtube.py`, `services/youtube_upload.py`) and tested:

- Public is not offered. Choosing Public in the checklist means the upload is refused.
- The approval is tied to a fingerprint of file hash, title, description, tags, visibility, audience
  and channel. If anything changes between draft, approval and upload, nothing is sent and you must
  approve again. The worker re-hashes the file and re-reads the channel just before uploading.
- A failed approved upload can be retried; it is re-verified each time. Uploading the same file twice
  needs an explicit "upload again" tick.
- A studio-side daily limit (`IFTYEDITS_YOUTUBE_DAILY_UPLOAD_LIMIT`, default 5, 0 = off) protects quota.
- Quota, rate-limit, auth, no-channel and network errors are classified and shown in plain language.

## Setup (once)

1. In Google Cloud Console create a project, enable **YouTube Data API v3**, configure the OAuth
   consent screen (add yourself as a test user while the app is in Testing) and create an OAuth client
   of type **Desktop app**. Download its JSON.
2. Save the JSON **outside the repository** and set in `.env`:
   `IFTYEDITS_YOUTUBE_CLIENT_SECRETS=/absolute/path/client_secret.json`
3. Restart the studio, open **Settings → YouTube** and press **Connect YouTube account**. Your browser
   opens Google's sign-in; approve upload access and read-only channel access (the second is only used
   to show which channel will receive the video).

Scopes: `youtube.upload` and `youtube.readonly`. Sign-in uses the system browser, a `127.0.0.1`
loopback redirect, PKCE and a random `state` check.

## Where credentials live

- Client file: wherever you saved it (never committed; `.gitignore` also blocks `client_secret*.json`).
- Token: `~/.config/iftyedits/youtube_token.json` (override with `IFTYEDITS_YOUTUBE_TOKEN_PATH`), mode
  `0600` in a `0700` folder, written atomically. A token path inside the project folder is refused.
- Tokens and secrets are never logged, never put in error messages and never stored in SQLite (a test
  scans the database dump). **Disconnect** deletes the file and asks Google to revoke the token.

## Quota and limits (researched 2026-11-22; sources disagree, so check the console)

- Google's current `videos.insert` reference lists the cost as about 1 unit (it was about 1600 until
  December 2025, and the Quota Calculator page still shows 1600). The default project quota is 10,000
  units/day. Uploads also fall under a separate "Video Uploads" quota bucket (about 100 per day).
- The old rule that unverified API projects upload only as private appears reversed in the revision
  history (2 November 2026), but the YouTube Help page still describes it. The studio uploads
  private/unlisted anyway.
- Do not rely on these numbers: the studio reports the real error YouTube returns.
  Sources: developers.google.com/youtube/v3/revision_history, /docs/videos/insert, /determine_quota_cost.

## Data

Migration `0010_youtube.sql` adds `youtube_uploads` (status, the approved details as JSON, fingerprint,
bytes sent, YouTube video id, error kind and message). Jobs use kind `upload_youtube`.

## Limitations

- **Not validated against the real YouTube API**: there is no OAuth client file here, so every test uses
  a local stub of Google's token, channel and resumable-upload endpoints. Real-world behaviour
  (consent-screen steps, token expiry for apps in Testing mode, quota numbers) is unconfirmed.
- Only MP4 exports; no thumbnails upload, no playlists, no scheduling, no editing of uploaded videos.
- One channel per sign-in (the first channel returned). Brand-account selection is not implemented.
- Needs the background worker (`iftyedits ui` starts one) and keeps running until the upload finishes.
