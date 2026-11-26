# User Guide

IftyEdits AI Studio will turn long-form videos into YouTube Shorts. This guide covers what works
**today**:

- **Import** (Phase 1A): copy videos into a protected library and inspect them.
- **Media preparation** (Phase 1B): extract 16 kHz speech audio, make a 720p preview proxy and a
  thumbnail, in the background.
- **Transcription** (Phase 1C): turn speech into text with timestamps, on this Mac, then read,
  search and export it as SRT, WebVTT, text or JSON.

Clip finding, Short editing and export, YouTube-link import and one-click production
(Phases 2 to 4) also work and are described below. Uploading and metadata generation are
planned (see [ROADMAP.md](ROADMAP.md)) and are not available yet.

## 1. Set up

You need a Mac (Apple Silicon recommended), [uv](https://docs.astral.sh/uv/) and FFmpeg:

```bash
brew install ffmpeg      # provides ffmpeg and ffprobe
```

Then, in the project folder:

```bash
uv sync                  # installs Python 3.12, the app and faster-whisper into .venv
cp .env.example .env     # optional; edit to change folders, limits or the default model
uv run iftyedits check   # confirms ffprobe, ffmpeg, faster-whisper, models and the database
```

`iftyedits check` prints the data folder, the ffprobe and ffmpeg paths, the H.264 encoder it will
use, the faster-whisper version, which speech models are downloaded, the database path with its
schema version, and any running workers. It exits with an error if something required is missing.
A note that the default speech model "is not downloaded" is not an error; see section 5.

## 2. Launch the app

```bash
uv run iftyedits ui
```

This starts two things:

- the dashboard at <http://127.0.0.1:8501> (only reachable from this Mac), and
- a background **worker** that does the actual work: copying, FFmpeg processing, model
  downloads and transcription.

Stop both with Ctrl-C. Options: `--port N` to use another port, `--no-worker` to start only the
dashboard.

If the dashboard is already running, the command starts nothing and tells you whether a worker is
active. To start just a worker for it, run `uv run iftyedits ui --start-worker`; the worker begins
processing every queued job.

You can also run them in two terminals:

```bash
uv run iftyedits worker
uv run streamlit run app/Home.py
```

If no worker is running, jobs wait in the queue. Every page warns you when that happens.

After you update the code (for example `git pull`), stop the app with Ctrl-C and start it
again. A dashboard or worker that was already running keeps the old code in memory and can
fail with `ImportError` on new pages or job types. Run `iftyedits` commands from the project
folder (or set `IFTYEDITS_HOME`); from another folder they open a different, empty `data/`.

## 3. The dashboard

### Navigation

The sidebar lists the everyday sections in workflow order: **Dashboard**, **Create Shorts**,
**My Videos**, **Clip Editor**, **Finished Shorts** and **Analytics**. Under **More** are
**Processing Queue**, **Settings** and **Guides & Help**. **My Videos** holds the video list
(Videos tab) and Transcripts; **Clip Editor** holds Review candidates, Edit Short and the upload
package. **Edit** on a candidate opens it in the Edit Short step.

### Dashboard

- Two shortcuts: **Make Shorts** and **Watch and download**.
- Worker status: **Ready**, the number of jobs in progress, or a warning when no worker runs.
- A notice when videos still need media preparation or a preparation stage failed.
- Recent videos and recent exports.
- **Studio details** (folded away): a seven-step tracker for your newest video, counters, and
  where your data, originals, exports, models and database are.

### Guides & Help

A short in-app walkthrough (sidebar: **Guides & Help**). It can be reopened any time; the
welcome box on the Dashboard has a **Hide this welcome** button that stays hidden.

### Import: YouTube link (Phase 4)

1. Open **Create Shorts**, tab **YouTube link**. Paste a watch, youtu.be or Shorts link. Playlists,
   channels and live streams are refused.
2. Tick **I have permission to use this video**. The app cannot know your rights; downloading
   does not make reuse allowed, and it never uses logins, cookies or DRM workarounds. Private,
   age-restricted, members-only or region-locked videos fail with a message; use a local file.
3. Choose how many Shorts, the minimum score and a caption style.
4. **Produce Shorts** runs the whole pipeline in one job. **Download only** just fetches and
   imports the video. A link already downloaded is reused, not downloaded again.
5. Follow it on **Processing Queue**; when finished, the per-clip summary shows status, length, size and
   output path. **Finished Shorts** shows every finished MP4 as a gallery card with a Preview button and its file location.

The same pipeline is available from the **Library** page (**Produce Shorts** per video) and
the CLI (`iftyedits produce`). Each clip is its own file; one failing clip does not affect the
others. Cancelling stops between steps and keeps files already exported. Clips are checked
with ffprobe after rendering (1080x1920, H.264, audio when the source has audio).

### Import: local file

1. Type the full path of a video file or a folder on this Mac, for example
   `/Users/you/Movies/Episode 12.mp4`. There is no browser upload: large files are read from
   disk directly.
2. Tick **Include subfolders** to scan folders recursively.
3. Click **Scan**. The page lists the video files it found with their sizes. Anything in a folder
   that will not be imported is listed under **Skipped items and reasons** (symbolic links,
   hidden files and folders, unsupported extensions, subfolders without recursion, special files).
4. Leave **Prepare media after import** ticked (the default) to queue audio, proxy and thumbnail
   generation as soon as each import finishes.
5. Click **Queue N file(s) for import**. Files already queued or running are not queued twice.
6. The **Jobs** table refreshes every two seconds.

Accepted extensions by default: `.mp4 .m4v .mov .mkv .webm .avi .mts .m2ts .mxf`. A file must
also contain a real video stream: audio-only files, text files and corrupt files are rejected
before anything is copied. Paths inside the app's own data folder are refused.

### Library

- A table of imported videos with duration, resolution, size, status, sources, import time,
  hash, one column per preparation stage (**Audio**, **Proxy**, **Thumbnail**) and a
  **Transcript** column (`· not started`, `⏳ queued`, `⏳ running`, `✅ done`, `✅ no speech`,
  `❌ failed`, `⏹ cancelled`).
- **Details**: pick a video to see duration, size, container, status and whether it has a
  transcript. The **Media preparation** panel (refreshed every two seconds) shows the thumbnail,
  the progress of a running job with **Cancel processing**, the stage table with errors, and the
  generated files. **Prepare media** runs the missing stages and reuses valid outputs;
  **Regenerate all** runs every stage again.
- Streams, SHA-256 and vault location, and every path the video was imported from.

### Transcripts

This page has four parts.

**Speech models** (open automatically until a model is installed). A table lists the supported
models with their download size, approximate memory use and status. Choose a model under
**Model to download**; the caption shows its exact download size, where it comes from
(huggingface.co, at a fixed revision), the free disk space and the folder it will be saved in.
Click **Download small (463.7 MB)** (the label shows the chosen model and size). The download
runs in the worker with a progress bar and a **Cancel … download** button. Each file is checked
against its expected size, and the weights against their published SHA-256, before the model is
installed. A failed download shows its error and leaves nothing half-installed.

**Transcribe**. Pick a **Video** at the top of the page, then choose:

- **Model**: only downloaded models are listed. `small` is the default.
- **Language**: **Auto-detect**, or the spoken language if you know it (more reliable for short
  or mixed-language videos).
- **Transcribe again**: run the model even if a transcript with the same settings exists.

Click **Transcribe**. The panel shows the job's progress ("Transcribing 1:23 of 10:00") and a
**Cancel transcription** button, and refreshes every two seconds. When the job finishes, the
transcript appears below. If it fails, the error is shown in red with a hint; fix the cause and
press **Transcribe** again to retry. Videos without an audio track show a warning instead of the
button.

Transcription uses the 16 kHz audio from media preparation. If that audio is missing or damaged,
the job regenerates it from the original first, so you do not have to prepare media beforehand.

**Transcript**. Shows the model, language (with detection confidence), number of segments and
words, and processing time. If you transcribed the video more than once with different settings,
**Version** lets you look at each one; ★ marks the one later steps will use, and **Use this
version** changes it. Below:

- a table of segments with **Start**, **End** (minutes:seconds.milliseconds) and **Text**;
- **Word timings**: pick a segment to see every word with its start, end and confidence;
- **Download SRT**, **Download VTT**, **Download TXT** and **Download JSON** buttons.

A warning appears when automatic language detection was uncertain (below 60%). "No speech was
detected" means the model found no words; try a larger model or set the language.

**Search**. Type words into **Find words in transcripts**. Every word must appear in the same
segment; matching ignores capitals and accents and also matches word beginnings (`tal` finds
`talks`). Choose **This video** or **All videos**. Results show the time stamp, video number and
the matching text in bold. Only the current (★) transcript of each video is searched.

### Clips

Finds moments in a transcribed video that may work as Shorts. It needs the video's transcript
(see Transcripts) and a running worker (`uv run iftyedits ui` starts one).

1. Pick a **Video**. Videos without a transcript show a warning.
2. Press **Find clips**. The panel shows the job's progress and a **Cancel analysis** button.
   **Analyse again** recomputes even when nothing changed.
3. When the job ends, **Suggestions** lists up to 20 ranked clips. Pressing **Update
   suggestions** later re-runs the analysis; it is reused instantly if nothing changed.

Use **Sort by** (rank, score, evidence quality, duration, position), **Moment type**,
**Status** (hides rejected by default), **Minimum score** and the length limits to narrow the
list. Pick a clip under **Inspect** to see:

- the time span, moment type, score and how much evidence supports it;
- why it was chosen, its strengths and weaknesses (strengths are only positive evidence;
  penalties such as **Channel boilerplate**, meaning intro, outro or "like and subscribe" wording,
  are listed first under weaknesses), and the transcript excerpt;
- the **score breakdown**: each factor's value (0-100), its weight and the reason. A weight of
  0 means it was not assessed (for example wording cues in a non-English transcript);
- **Evidence signals**: transcript, audio and scene signals inside the clip, each with its
  source, strength and reliability.

**Favorite**, **Reject** and **Clear decision** are saved in the database. They survive
restarts and re-analysis, and nudge the next ranking (favorites up, rejected down, and a small
bias toward moment types you favor). Nothing is cut, rendered or uploaded on this page.

The score is an algorithmic recommendation based on the transcript, loudness and scene
changes. It does not predict views and does not check copyright, music rights or platform
rules. Faces and speakers do not affect the score.

### Short editor

Turns a suggested clip into a vertical MP4. Open it from **Clips** with **Edit as a Short**, or
pick a video and clip on the page. Nothing here changes your original, the transcript or the
suggestion; edits are saved as settings for that one Short.

- **Trim**: start and end in seconds (3 to 180 s, inside the video). **Reset to the AI
  suggestion** restores the original range.
- **Framing**: a 0 to 100 crop position (50 = centre). The crop keeps the full height and is
  never stretched. The page says which mode is active: *Auto-follow* (a smooth moving crop from
  **Auto-frame**) or *Manual or steady*. Moving the slider or changing the trim switches to
  manual.
- **Face-aware cropping**: install the model once with
  `uv run iftyedits models download yunet` (227 KB). **Auto-frame** and one-click production then
  follow faces, jump at scene cuts and pan slowly inside a shot. When two people do not fit, the
  window stays on one until the other is clearly larger; it does not know who is speaking. Faces
  shorter than 8% of the picture height are ignored, and clips with faces in fewer than a quarter
  of the checked pictures are framed by picture detail. No one is identified and nothing about
  faces is stored. Set `IFTYEDITS_FACE_FRAMING=false` to turn it off. Shorts you already framed
  keep their crop until you press **Auto-frame** again.
- **Captions**: on or off, presets (Classic, Boxed, Highlight, Minimal, Impact, Studio, Clean, Punchy, Cinematic, Mobile), key-word colouring, bold, size, colours,
  outline, background, alignment and position. The text box lists each caption line so you can
  correct wording; corrections are stored with the Short, not in the transcript. Highlight
  only works when the transcript has word timing.
- **Look and sound**: fade in/out, brightness, contrast, saturation; keep or drop audio,
  volume, sound fades, and optional loudness evening (about -16 LUFS).
- **Output**: 1080x1920 (recommended), 720p or 540p, and quality. Smaller sources are scaled
  up; no detail is added.
- **Render preview video** makes a small 540x960 clip with the same crop, captions and sound.
  **Export Short** makes the finished file in `data/exports/shorts/` and never overwrites an
  earlier one. Both run in the worker; a second click while one runs is refused.
  **Reset everything** returns every setting to its default.

### Jobs

A failed or cancelled production has a **Retry** button that queues the same production again
(finished stages and exports are reused).

Active jobs (imports, media preparation, transcription and model downloads) with progress bars
and **Cancel** buttons, then the job history with outcomes, and an **Errors** list. Refreshed
every two seconds.

## 4. Command line

Run any command as `uv run iftyedits <command>`. Use `--help` after a command for details.

| Command | What it does |
|---|---|
| `import PATH... [-r]` | Import files or folders now, in this terminal |
| `enqueue PATH... [-r] [--process]` | Queue files or folders for the worker; `--process` also prepares media |
| `worker [--once]` | Run queued jobs; `--once` exits when the queue is empty |
| `process VIDEO... \| --all [--stage S] [--force]` | Queue media preparation |
| `models [list]` | List speech and face models, sizes and whether they are downloaded |
| `models download NAME` | Download and verify a model now (speech, or `yunet` for faces) |
| `models verify NAME` | Re-hash an installed model |
| `transcribe VIDEO... \| --all [--model M] [--language L] [--force]` | Queue transcription |
| `transcript VIDEO [--format txt\|srt\|vtt\|json] [--save]` | Print the current transcript, or save it under `data/exports/transcripts/` |
| `analyze VIDEO... \| --all [--force]` | Queue clip analysis of transcribed videos |
| `clips list VIDEO [--sort S] [--type T] [--min-score N] [--status S]` | List ranked suggestions |
| `clips show VIDEO REF` | Show one suggestion with its score breakdown |
| `clips favorite\|reject\|clear VIDEO REF` | Save or remove your decision |
| `search QUERY [--video VIDEO] [--limit N]` | Search current transcripts |
| `jobs [--limit N] [--active]` | List jobs |
| `cancel JOB_ID...` | Cancel queued jobs or stop running ones |
| `artifacts VIDEO [--verify]` | Show preparation stages and generated files |
| `list [--limit N]` | List imported videos |
| `show VIDEO [--verify]` | Show one video; `--verify` re-hashes the vault copy |
| `reconcile` | Record vault files that have no database entry |
| `check` | Verify tools, engine, models, database and workers |
| `ui [--port N] [--address A] [--no-worker \| --start-worker]` | Start the dashboard and a worker |

`VIDEO` is an ID (e.g. `3`) or a SHA-256 prefix of at least 6 characters.

Examples:

```bash
uv run iftyedits models download small
uv run iftyedits transcribe 3 --language en
uv run iftyedits worker --once
uv run iftyedits transcript 3 --format srt > ~/Desktop/episode.srt
uv run iftyedits search "giveaway"
```

## 5. Speech models

Models are downloaded **only when you ask** (Transcripts page or `models download`). They are
saved in `models/faster-whisper/<name>/` (change with `IFTYEDITS_MODELS_DIR`) and never committed
to Git. A transcription job never downloads anything; if its model is missing it fails with a
message telling you what to download.

| Model | Download | Notes |
|---|---|---|
| `tiny` | 74 MB | Fastest, least accurate |
| `base` | 141 MB | Fast, basic accuracy |
| `small` | 464 MB | Balanced; the default |
| `medium` | 1.4 GB | More accurate, slower |
| `large-v3-turbo` | 1.5 GB | Near large-v3 accuracy, faster than large-v3 |
| `large-v3` | 2.9 GB | Most accurate, slowest |

On Apple Silicon the engine (faster-whisper / CTranslate2) runs on the CPU with 8-bit weights.
Its experimental Apple GPU mode was measured slower on an M1 Max, so it is not used. Measured speeds are in [DECISIONS.md](DECISIONS.md) (D16).

Change the default model or language in `.env`:

```bash
IFTYEDITS_TRANSCRIPTION_MODEL=small
IFTYEDITS_TRANSCRIPTION_LANGUAGE=auto
```

## 6. Statuses and what to do

| You see | Meaning | What to do |
|---|---|---|
| `queued` | Waiting for the worker | Start a worker if the page says none is running |
| `running` / `cancelling` | In progress / stopping | Wait, or cancel |
| `succeeded` | Done | Read the result |
| `failed` | Stopped with an error | Read the error, fix the cause, start it again |
| `cancelled` | Stopped by you | Start it again when you want |
| "Speech model 'small' is not downloaded" | No model yet | Download it on the Transcripts page |
| "This video has no audio track" | Nothing to transcribe | None |
| "The prepared audio file is unreadable" | Damaged audio | Regenerate media, or transcribe again (it regenerates damaged audio) |
| "Could not download model …" | Network problem (each file is retried 4 times and resumed) | Check the connection; press Download again |
| "Transcription took longer than the configured limit" | Hit `IFTYEDITS_TRANSCRIPTION_TIMEOUT_S` | Use a smaller model or raise the limit |

A worker that stops unexpectedly (crash, shutdown) returns its running job to the queue, and a new
worker resumes it. A transcript is saved only when the whole run succeeds, so an interrupted or
failed run never leaves a partial transcript and never removes an earlier one.

## 7. How your originals are protected

- **Your file is only read.** It is never modified, moved, renamed, deleted or linked.
- **The library keeps its own copy** in the vault at
  `data/originals/<first two hash characters>/<sha256><extension>`, read-only.
- **Changes during copy are caught**, **identical content is stored once**, **disk space is
  reserved**, and **interruptions are safe** (see the README for details).
- **Generated files never touch the vault.** Audio, proxies and thumbnails go to `data/derived/`,
  saved transcript exports to `data/exports/transcripts/`, speech models to `models/`. Every write
  is checked against these folders first.
- **Transcription only reads** the prepared audio. Transcripts are stored in the database.
- **Nothing deletes originals automatically.** There is no delete command.

To confirm a video's vault copy is unchanged at any time:

```bash
uv run iftyedits show 3 --verify      # prints "integrity   OK"
shasum -a 256 "/path/to/your/original.mp4"   # equals the SHA-256 shown by `show`
```

Data folders (never committed to Git):

| Folder | Contents |
|---|---|
| `data/originals/` | Read-only vault copies |
| `data/staging/` | Copies in progress |
| `data/derived/` | Audio, proxies and thumbnails |
| `data/exports/` | Saved transcript exports (`transcript --save`) |
| `data/db/` | SQLite database, including transcripts |
| `models/` | Downloaded speech models |

## 8. Current limitations

- Entertainment quality and real-world multilingual performance are not yet fully validated. All
  speech tests so far used synthetic voices (Spanish and German included) and generated video;
  no real podcast, sports, slides, noisy-recording or multilingual footage has been run. Clip
  suggestions are scored by explainable signals, not by how entertaining a clip is.
- There is no uploading. Metadata and review need an optional AI model (Phase 6). HDR video is not tone-mapped. Automatic
  framing follows faces (with the face model) or image detail; it does not know who is speaking
  and has no split-screen layout for gameplay with a webcam. Adjust the crop in the Short editor.
- Link ingestion depends on yt-dlp and YouTube; if YouTube changes, update yt-dlp. Renders are made one at a time.
- Clip scoring uses English wording cues (including boilerplate detection), transcript timings, loudness and scene changes.
  Scoring does not analyse faces, speakers or tone of voice, and suggestions can be previewed and
  trimmed in the Short editor.
- Transcription runs on the CPU. On an M1 Max, `tiny` ran about 33x and `base` about 20x faster
  than real time; `small` is slower (not yet measured). See D16 in [DECISIONS.md](DECISIONS.md).
- One language per transcript. Speech that switches languages is transcribed in the detected
  (or chosen) language. There is no speaker separation.
- Search matches whole words and word beginnings, separated by spaces or punctuation. Languages
  written without spaces (Chinese, Japanese, Thai) only match whole runs of text.
- Cancelling a transcription takes effect after the current segment (usually a few seconds).
- Downloads cannot be paused; a cancelled or failed download starts again from the beginning.
- Duplicates are detected only for byte-identical files.
- The worker runs one job at a time.
- Videos cannot be deleted from the library through the app.
- The dashboard is local-only (`127.0.0.1`) and has no login.

### Processing Queue and Settings (Phase 5)

**Processing Queue** counts running, queued, completed, failed and cancelled jobs (among the
latest 100), lists active jobs with Cancel, and keeps Retry for failed or cancelled productions.
**Settings** shows free disk space, where data is stored, the transcription and clip settings in
effect, and diagnostics (FFmpeg, FFprobe, worker, database version). Values come from
`IFTYEDITS_` environment variables or `.env`; the page does not edit them. Its one switch hides
or shows the Dashboard welcome card.

## Analytics (optional)

**Analytics** shows how your uploaded Shorts perform. It only reads from YouTube.

1. Connect your account in Settings (same connection as uploading).
2. Uploads made by the studio appear by themselves. For a Short you uploaded by hand, open **Add a
   video you uploaded yourself**, paste its link, and pick the Short it came from if you know it.
3. Press **Refresh numbers**. Views, likes and comments are lifetime totals at that moment. Only
   videos on your connected channel are read; others are reported and skipped.
4. For watch time, average percentage watched and subscribers gained, open **Allow detailed
   analytics** and sign in again. The YouTube Analytics API must be enabled in your Google Cloud
   project ([YOUTUBE.md](YOUTUBE.md)). Choose a window of 7, 28, 90 or 365 days. These numbers
   arrive a few days late.

**What seems to work** groups your videos by moment type, caption style, length and framing, and
shows medians with the number of videos behind each. It compares groups only when at least two
hold five videos each. A pattern is not proof that the choice caused it, and nothing changes
ranking or publishing automatically. Numbers are stored on this computer; **Stop following** a
video deletes its stored numbers. If you reconnect in Settings, allow detailed analytics again.

The same is available as `iftyedits analytics [list|add|refresh]`.

## Upload package (Phase 6)

Open **Clip Editor → Upload package**, or **Finished Shorts → Review upload package**. Pick a
Short, then work through the tabs: **Metadata** (generate or write title, description, hashtags,
search tags; copy boxes), **Thumbnail** (pick a real frame), **Safety review** (run the advisory
review or record your own), **Rights** (what you know about the source), **Checklist** (your
choices and confirmations, then the final confirm), **Package** (save JSON or TXT).

AI is off until you set up a local model or a Claude API key (Settings → AI writing and review
shows how and lets you choose Local, Claude or Hybrid, with monthly limits and usage). Without
it you write everything yourself. Each result shows which provider wrote it. The studio never uploads. "Ready for upload (you confirmed)" only means
you finished the checklist; it is not legal or policy clearance. Details: [PUBLISHING.md](PUBLISHING.md).

## Uploading to YouTube (optional, Phase 7)

Settings → YouTube explains the one-time setup (an OAuth client file from Google Cloud). Once connected
and the checklist is confirmed, **Clip Editor → Upload package → YouTube → Review upload details**
shows the exact channel, file, title, description, tags and visibility. Type `I approve this upload` to
queue it. It uploads as Private or Unlisted only; open it in YouTube Studio to check and publish it
yourself. Change anything after reviewing and you must review again. Without setup, use the Package
tab to copy everything by hand. Details: [YOUTUBE.md](YOUTUBE.md).
