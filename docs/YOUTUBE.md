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

## Analytics (read only)

The Analytics page uses two Google sources.

| Numbers | API | Permission |
|---|---|---|
| Views, likes, comments, visibility | Data API `videos.list` (1 quota unit per 50 videos) | `youtube.readonly`, already granted |
| Watch time, average view duration and percentage, shares, subscribers gained | YouTube Analytics API `reports.query` | `yt-analytics.readonly`, asked for only when you choose it |

For the second row: in Google Cloud, open **APIs & Services → Library**, enable **YouTube
Analytics API**, add the `yt-analytics.readonly` scope to the consent screen if the console asks,
then use **Analytics → Allow detailed analytics**. Without it the page still shows the first row.
Reconnecting from Settings grants only the upload permissions again.

The report is requested for `channel==MINE`, one row per video, at most 200 videos per request.
YouTube omits the rows when it has no data yet. References: developers.google.com/youtube/analytics
(reports.query, channel reports) and developers.google.com/youtube/v3/docs/videos/list, read
2026-11-27. **Not verified against real Google**; only the local stub and tests exercise it.

## Trying it without Google (local stub)

`scripts/dev_youtube_stub.py` is a development-only stand-in for Google's sign-in, channel and
resumable-upload endpoints. It lets you walk the whole approval flow in the browser without a Google
Cloud project. It is **not** Google: passing here proves the studio's own logic, not real YouTube
behaviour.

```bash
uv run python scripts/dev_youtube_stub.py --port 8700 --delay 0.15
```

Point the studio at it with a client file whose `auth_uri`/`token_uri` use `http://127.0.0.1:8700/...`
and these settings (loopback addresses are the only non-Google hosts the studio accepts):

- `IFTYEDITS_YOUTUBE_CLIENT_SECRETS`, `IFTYEDITS_YOUTUBE_TOKEN_PATH` (outside the project)
- `IFTYEDITS_YOUTUBE_API_BASE=http://127.0.0.1:8700/youtube/v3`
- `IFTYEDITS_YOUTUBE_UPLOAD_BASE=http://127.0.0.1:8700/upload/youtube/v3`
- `IFTYEDITS_YOUTUBE_ANALYTICS_BASE=http://127.0.0.1:8700/v2`

The stub's `/admin?channel=<id>&fail=quota|none` changes the signed-in channel or makes the next
upload fail with a quota error, to test channel-mismatch refusal and retry.

Any other host in the client file or API settings is refused (`trusted_endpoint`), so a tampered client
file cannot send tokens elsewhere.

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
