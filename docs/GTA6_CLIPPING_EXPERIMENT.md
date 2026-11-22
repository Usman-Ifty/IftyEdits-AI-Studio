# GTA 6 clipping experiment (2026-11-22)

**Source used:** `VIDEOS/GTA 6_ Gameplay DETAILS WE FOUND.mp4`, a file already on the owner's disk
(906.3 s, 1280x720 H.264/AAC, 168 MB). It was imported through the normal vault (hash-verified copy;
the original is untouched).

**Reference link (`youtu.be/Ra6sNe8s2Hc`) was NOT fetched.** Downloading a third-party video needs
the owner's confirmation of rights, and a local file was available, so URL ingestion was not exercised.

## Pipeline run

`iftyedits import` then `iftyedits produce 824f2065 --clips 1 --min-length 20 --max-length 45`
(worker started by `iftyedits ui`). Succeeded; faster-whisper `small`-class local model transcribed it.
12 candidates were ranked; the top one was exported.

## Output

`data/exports/audit/gta6-test-short.mp4` (copy of the file the pipeline wrote). Verified with ffprobe/ffmpeg:

- 1080x1920 (9:16), H.264 30 fps, AAC 48 kHz stereo, 24.5 s, 43 MB.
- Source range 12:21–12:46. Mean volume −24.6 dB, peak −9.1 dB (audible, not clipped).
- Three frames viewed (1 s, 9 s, 20 s): captions burned in, readable, white with outline, lower third.

## Honest quality notes

- The source is a **commentary/analysis video**, not gameplay with funny moments or reactions, so the
  "funny gameplay" goal could not be met from it. The top pick is a talking-point question.
- Frame at 1 s shows a burned-in source subtitle cut at the left edge ("bodyguar…") because the 16:9
  frame was cropped to 9:16; the crop does not avoid existing on-screen text.
- Opening hook and "no dead time" were not judged beyond the three frames; audio was not listened to.
- No music added. Nothing uploaded or published.
