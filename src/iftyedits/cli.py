"""Command-line interface: ``iftyedits <command>``."""

from __future__ import annotations

import argparse
import http.client
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from types import FrameType

from iftyedits import __version__
from iftyedits.adapters.faster_whisper import engine_version
from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.adapters.model_store import (
    FACE_MODEL,
    build_face_model_store,
    build_model_store,
)
from iftyedits.config import Settings, project_home
from iftyedits.core.clip_models import ClipCandidate, ClipDecision
from iftyedits.core.errors import IftyEditsError
from iftyedits.core.models import (
    ALL_STAGES,
    CancelOutcome,
    Job,
    JobStatus,
    ModelState,
    Stage,
)
from iftyedits.core.scoring import CATEGORY_LABELS, SCORING_VERSION
from iftyedits.core.shorts import CAPTION_PRESETS
from iftyedits.core.sources import PERMISSION_NOTICE, parse_video_url
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.analytics import build_analytics_service
from iftyedits.services.ingest import IngestService, build_vault, discover, validate_source
from iftyedits.services.production import ProductionOptions
from iftyedits.services.subtitles import EXPORT_FORMATS, export
from iftyedits.services.transcription import options_from_settings, transcription_blocker
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.db import open_database, open_read_only, schema_version
from iftyedits.storage.originals import ProgressCallback, assert_safe_output_path, sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui.analytics import video_rows
from iftyedits.ui.clips import (
    DECISION_FILTERS,
    SORT_OPTIONS,
    ClipFilter,
    category_label,
    clip_row,
    component_rows,
    confidence_band,
    decision_label,
    filter_candidates,
    score_band,
    signal_row,
    signals_for,
    sort_candidates,
    span_label,
)
from iftyedits.ui.formatting import (
    human_duration,
    human_size,
    job_row,
    model_row,
    search_row,
    stage_row,
    stream_row,
    transcript_facts,
)

logger = logging.getLogger("iftyedits")

LOCALHOST = "127.0.0.1"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
HEALTH_PATH = "/_stcore/health"
DASHBOARD_START_TIMEOUT_S = 60.0
DASHBOARD_POLL_S = 0.25


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _expand(settings: Settings, paths: Sequence[str], recursive: bool) -> tuple[list[Path], int]:
    """Turn file and folder arguments into files; print and count every skipped entry."""
    files: list[Path] = []
    skipped_count = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found = discover(settings, path, recursive=recursive)
            for skipped, reason in found.skipped:
                print(f"skip  {skipped}  ({reason})", file=sys.stderr)
            skipped_count += len(found.skipped)
            files.extend(found.files)
        else:
            files.append(path)
    return files, skipped_count


def _progress_printer(name: str) -> ProgressCallback:
    state = {"last": -1}

    def report(copied: int, total: int) -> None:
        percent = int(copied * 100 / total) if total else 100
        if percent >= state["last"] + 5 or percent == 100:
            state["last"] = percent
            print(f"\r  copying {name}: {percent:3d}%", end="", file=sys.stderr, flush=True)

    return report


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    """Import files synchronously in this process."""
    files, skipped = _expand(settings, args.paths, args.recursive)
    if not files:
        print("No video files found.", file=sys.stderr)
        return EXIT_FAILED
    failures = 0
    with closing(open_database(settings.db_path)) as conn:
        service = IngestService(
            settings,
            VideoRepository(conn),
            FFprobe(settings.ffprobe_path, settings.probe_timeout_s),
            build_vault(settings),
        )
        service.vault.clean_staging()
        for path in files:
            try:
                result = service.import_video(path, progress=_progress_printer(path.name))
            except IftyEditsError as exc:
                failures += 1
                print(f"\nFAIL  {path}: {exc}", file=sys.stderr)
                continue
            video = result.video
            print(
                f"\n{result.outcome.value:<9} #{video.id}  {video.sha256[:12]}  "
                f"{video.status.value}  {path}"
            )
    print(
        f"{len(files) - failures} succeeded, {failures} failed, {skipped} skipped",
        file=sys.stderr,
    )
    return EXIT_FAILED if failures else EXIT_OK


def cmd_enqueue(settings: Settings, args: argparse.Namespace) -> int:
    """Queue files for the background worker."""
    files: list[Path] = []
    expanded, _ = _expand(settings, args.paths, args.recursive)
    for path in expanded:
        try:
            files.append(validate_source(settings, path))
        except IftyEditsError as exc:
            print(f"skip  {path}: {exc}", file=sys.stderr)
    with closing(open_database(settings.db_path)) as conn:
        ids = JobQueue(conn).enqueue_imports(files, process_after=args.process)
    print(f"Queued {len(ids)} job(s); {len(files) - len(ids)} already queued.")
    return EXIT_OK if files else EXIT_FAILED


def cmd_process(settings: Settings, args: argparse.Namespace) -> int:
    """Queue media preparation (audio, proxy, thumbnail) for imported videos."""
    stages = [Stage(s) for s in args.stage] if args.stage else list(ALL_STAGES)
    with closing(open_database(settings.db_path)) as conn:
        repo = VideoRepository(conn)
        if args.all:
            videos = repo.list_recent(limit=1_000_000)
        elif args.videos:
            videos = [repo.resolve_reference(ref) for ref in args.videos]
        else:
            print("Name one or more videos, or use --all.", file=sys.stderr)
            return EXIT_USAGE
        queue = JobQueue(conn)
        for video in videos:
            job_id, created = queue.enqueue_processing(video.id, stages, force=args.force)
            state = "queued" if created else "already queued"
            print(f"job {job_id:<5} {state:<15} #{video.id}  {video.original_filename}")
    if not videos:
        print("No videos imported yet.")
    return EXIT_OK


def cmd_fetch(settings: Settings, args: argparse.Namespace) -> int:
    """Queue the download of a YouTube video you are allowed to use."""
    try:
        url = parse_video_url(args.url)
    except IftyEditsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(PERMISSION_NOTICE, file=sys.stderr)
    with closing(open_database(settings.db_path)) as conn:
        job_id, created = JobQueue(conn).enqueue_url_fetch(url.canonical, process=args.process)
    print(f"job {job_id}: {'queued' if created else 'already queued'}  {url.canonical}")
    print("Run `iftyedits worker --once` to process the queue.")
    return EXIT_OK


def cmd_produce(settings: Settings, args: argparse.Namespace) -> int:
    """Queue the whole pipeline (fetch, transcribe, analyse, render) for a link or a video."""
    try:
        options = ProductionOptions(
            max_clips=args.clips,
            min_score=args.min_score,
            caption_preset=args.captions,
            model=args.model,
            language=args.language,
            force=args.force,
            min_duration_s=args.min_length,
            max_duration_s=args.max_length,
            review_only=args.review,
        ).validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        if "/" in args.source or "." in args.source:
            try:
                url = parse_video_url(args.source)
            except IftyEditsError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            print(PERMISSION_NOTICE, file=sys.stderr)
            job_id, created = queue.enqueue_production(options.as_payload(), url=url.canonical)
            label = url.canonical
        else:
            try:
                video = VideoRepository(conn).resolve_reference(args.source)
            except IftyEditsError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            job_id, created = queue.enqueue_production(options.as_payload(), video_id=video.id)
            label = f"#{video.id}  {video.original_filename}"
    print(f"job {job_id}: {'queued' if created else 'already queued'}  {label}")
    print("Run `iftyedits worker --once` to process the queue.")
    return EXIT_OK


def cmd_jobs(settings: Settings, args: argparse.Namespace) -> int:
    """Print recent (or only active) jobs."""
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        jobs = queue.list_active() if args.active else queue.list_recent(args.limit)
    for job in jobs:
        row = job_row(job)
        print(
            f"{job.id:<5} {job.kind:<13} {row['Status']:<17} {job.progress * 100:5.1f}%  "
            f"{row['File']}  {row['Detail']}".rstrip()
        )
    if not jobs:
        print("No active jobs." if args.active else "No jobs yet.")
    return EXIT_OK


def cmd_cancel(settings: Settings, args: argparse.Namespace) -> int:
    """Cancel queued jobs or ask the worker to stop running ones."""
    failed = False
    with closing(open_database(settings.db_path)) as conn:
        queue = JobQueue(conn)
        for job_id in args.job_ids:
            outcome = queue.request_cancel(job_id)
            text = {
                CancelOutcome.CANCELLED: "cancelled",
                CancelOutcome.REQUESTED: "cancellation requested; the worker will stop it",
                CancelOutcome.NOT_ACTIVE: "already finished; nothing to cancel",
                CancelOutcome.NOT_FOUND: "no such job",
            }[outcome]
            failed = failed or outcome is CancelOutcome.NOT_FOUND
            print(f"job {job_id}: {text}")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_artifacts(settings: Settings, args: argparse.Namespace) -> int:
    """Print processing stages and generated files for one video."""
    with closing(open_database(settings.db_path)) as conn:
        video = VideoRepository(conn).resolve_reference(args.video)
        processing = ProcessingRepository(conn)
        stages = processing.stages(video.id)
        artifacts = processing.artifacts(video.id)
    print(f"video       #{video.id}  {video.original_filename}")
    print("stages")
    if not stages:
        print("  none yet (run `iftyedits process` to prepare this video)")
    for stage in stages:
        row = stage_row(stage)
        print(
            f"  {row['Stage']:<10} {row['Status']:<10} {row['Finished (UTC)']:<20} {row['Detail']}"
        )
    print("artifacts")
    intact = True
    for artifact in artifacts:
        path = settings.derived_dir / artifact.relpath
        line = f"  {artifact.kind.value:<14} {human_size(artifact.size_bytes):>9}  {path}"
        if args.verify:
            ok = path.is_file() and sha256_file(path) == artifact.sha256
            intact = intact and ok
            line += "  OK" if ok else "  MISMATCH OR MISSING"
        print(line)
    if not artifacts:
        print("  none")
    return EXIT_OK if intact else EXIT_FAILED


def cmd_analytics(settings: Settings, args: argparse.Namespace) -> int:
    """Follow uploaded videos and fetch their numbers (read only on YouTube)."""
    with closing(open_database(settings.db_path)) as conn:
        service = build_analytics_service(settings, conn)
        if args.action == "add":
            if not args.video:
                print("Name a video link or id: iftyedits analytics add <link>", file=sys.stderr)
                return EXIT_USAGE
            print(f"following {service.add_video(args.video, args.short)}")
            return EXIT_OK
        if args.action == "refresh":
            report = service.refresh(args.days)
            print(f"updated {report.updated} video(s), {report.detailed} with watch time")
            for video_id, reason in report.skipped:
                print(f"  skipped {video_id}: {reason}")
            if report.note:
                print(report.note)
            return EXIT_OK
        service.sync_uploads()
        videos = service.videos()
        if not videos:
            print("No videos are being followed.")
        for row in video_rows(videos):
            print(
                f"{row['Video'][:50]:<50} views {row['Views']:>7}  likes {row['Likes']:>6}  "
                f"watched {row['Avg watched']:>5}  {row['Link']}"
            )
    return EXIT_OK


def cmd_models(settings: Settings, args: argparse.Namespace) -> int:
    """List models, or download / verify one."""
    faces = build_face_model_store(settings)
    store = faces if args.name == FACE_MODEL else build_model_store(settings)
    if args.action == "list":
        print(f"models dir  {store.root}")
        for status in store.statuses():
            row = model_row(status)
            print(
                f"  {row['Model']:<15} {row['Status']:<18} {row['Download']:>9}  "
                f"RAM ~{row['RAM (approx.)']:<7} {row['Notes']}"
            )
        print(f"default     {settings.transcription_model}")
        face = faces.status(FACE_MODEL)
        print(f"faces dir   {faces.root}")
        print(
            f"  {FACE_MODEL:<15} {face.state.value:<18} "
            f"{human_size(face.spec.size_bytes):>9}  {face.spec.summary}"
        )
        return EXIT_OK
    if not args.name:
        print("Name a model, for example: iftyedits models download small", file=sys.stderr)
        return EXIT_USAGE
    if args.action == "verify":
        ok = store.verify(args.name)
        print(f"{args.name}: {'OK' if ok else 'MISSING, INCOMPLETE OR DAMAGED'}")
        return EXIT_OK if ok else EXIT_FAILED
    spec = store.spec(args.name)
    if store.status(args.name).state is ModelState.INSTALLED:
        print(f"{args.name} is already installed in {store.path(args.name)}")
        return EXIT_OK
    print(
        f"Downloading {spec.name} ({human_size(spec.size_bytes)}) from "
        f"huggingface.co/{spec.repo_id} @ {spec.revision[:12]} into {store.root}",
        file=sys.stderr,
    )
    state = {"last": -1}

    def report(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        if percent != state["last"]:
            state["last"] = percent
            print(f"\r  {percent:3d}%  {human_size(done)} of {human_size(total)}", end="",
                  file=sys.stderr, flush=True)  # fmt: skip

    status = store.install(args.name, progress=report)
    print(f"\n{status.spec.name} installed and verified in {status.path}")
    return EXIT_OK


def cmd_transcribe(settings: Settings, args: argparse.Namespace) -> int:
    """Queue transcription of imported videos."""
    options = options_from_settings(settings, model=args.model, language=args.language)
    store = build_model_store(settings)
    store.spec(options.model)
    with closing(open_database(settings.db_path)) as conn:
        repo = VideoRepository(conn)
        if args.all:
            videos = repo.list_recent(limit=1_000_000)
        elif args.videos:
            videos = [repo.resolve_reference(ref) for ref in args.videos]
        else:
            print("Name one or more videos, or use --all.", file=sys.stderr)
            return EXIT_USAGE
        if store.status(options.model).state is not ModelState.INSTALLED:
            print(
                f"Model {options.model} is not downloaded. Run "
                f"`iftyedits models download {options.model}` first.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        queue = JobQueue(conn)
        for video in videos:
            has_audio = any(s.codec_type == "audio" for s in repo.streams(video.id))
            blocker = transcription_blocker(video, has_audio)
            if blocker is not None:
                print(f"skip  #{video.id}  {video.original_filename}: {blocker}")
                continue
            job_id, created = queue.enqueue_transcription(
                video.id, model=options.model, language=options.language, force=args.force
            )
            state = "queued" if created else "already queued"
            print(f"job {job_id:<5} {state:<15} #{video.id}  {video.original_filename}")
    if not videos:
        print("No videos imported yet.")
    return EXIT_OK


def cmd_analyze(settings: Settings, args: argparse.Namespace) -> int:
    """Queue clip analysis of imported videos that have a transcript."""
    with closing(open_database(settings.db_path)) as conn:
        repo = VideoRepository(conn)
        if args.all:
            videos = repo.list_recent(limit=1_000_000)
        elif args.videos:
            videos = [repo.resolve_reference(ref) for ref in args.videos]
        else:
            print("Name one or more videos, or use --all.", file=sys.stderr)
            return EXIT_USAGE
        transcripts = TranscriptRepository(conn)
        queue = JobQueue(conn)
        for video in videos:
            if transcripts.current(video.id) is None:
                print(
                    f"skip  #{video.id}  {video.original_filename}: no transcript yet "
                    f"(run `iftyedits transcribe {video.id}`)"
                )
                continue
            job_id, created = queue.enqueue_clip_analysis(video.id, force=args.force)
            state = "queued" if created else "already queued"
            print(f"job {job_id:<5} {state:<15} #{video.id}  {video.original_filename}")
    if not videos:
        print("No videos imported yet.")
    return EXIT_OK


def _find_candidate(repo: ClipRepository, video_id: int, reference: str) -> ClipCandidate:
    """A suggestion of the current analysis by rank (``3``) or stable key."""
    analysis = repo.current(video_id)
    if analysis is None:
        raise IftyEditsError(f"Video #{video_id} has no clip analysis yet")
    candidates = repo.candidates(analysis.id)
    ref = reference.strip().lower()
    matches: list[ClipCandidate] = []
    if len(ref) >= 4:
        matches = [c for c in candidates if c.key.startswith(ref)]
    if not matches and ref.isdigit():  # a short number is a rank; keys can also be all digits
        matches = [c for c in candidates if c.rank == int(ref)]
    if len(matches) != 1:
        raise IftyEditsError(
            f"No unique suggestion matches {reference!r}; use a rank (1-{len(candidates)}) "
            "or a key prefix of at least 4 characters"
        )
    return matches[0]


def cmd_clips(settings: Settings, args: argparse.Namespace) -> int:
    """List, inspect and review the ranked clip suggestions of a video."""
    with closing(open_database(settings.db_path)) as conn:
        video = VideoRepository(conn).resolve_reference(args.video)
        repo = ClipRepository(conn)
        analysis = repo.current(video.id)
        if analysis is None:
            print(
                f"Video #{video.id} has no clip analysis yet. "
                f"Run `iftyedits analyze {video.id}` with a worker running.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        if args.action in ("favorite", "reject", "clear"):
            if not args.ref:
                print("Name a suggestion by rank or key.", file=sys.stderr)
                return EXIT_USAGE
            target = _find_candidate(repo, video.id, args.ref)
            decision = {
                "favorite": ClipDecision.FAVORITE,
                "reject": ClipDecision.REJECTED,
                "clear": None,
            }[args.action]
            updated = repo.decide(target.id, decision)
            label = decision_label(updated.decision) or "no decision"
            print(f"#{updated.rank} {updated.key}: {label}")
            return EXIT_OK
        if args.action == "show":
            if not args.ref:
                print("Name a suggestion by rank or key.", file=sys.stderr)
                return EXIT_USAGE
            return _print_clip(repo, _find_candidate(repo, video.id, args.ref), analysis.id)
        candidates = filter_candidates(
            repo.candidates(analysis.id),
            ClipFilter(
                categories=frozenset(args.type or ()),
                min_score=args.min_score,
                decision=args.status,
            ),
        )
        for c in sort_candidates(candidates, args.sort):
            row = clip_row(c)
            print(
                f"#{row['Rank']:<3} {row['Score']:>3}  {row['Start']:>9} - {row['End']:<9} "
                f"{row['Length (s)']:>3}s  {row['Type']:<22} {c.key[:8]}  {row['Title']}"
                + (f"  [{row['Status']}]" if row["Status"] else "")
            )
        if not candidates:
            print("No suggestions match.")
    return EXIT_OK


def _print_clip(repo: ClipRepository, c: ClipCandidate, analysis_id: int) -> int:
    print(f"#{c.rank}  {c.title}")
    print(f"key      {c.key}")
    print(f"type     {category_label(c.category)}")
    print(f"span     {span_label(c)}")
    print(f"score    {c.score:.0f}/100 ({score_band(c.score)}); {confidence_band(c.confidence)}")
    if c.decision:
        print(f"status   {decision_label(c.decision)}")
    print(f"\n{c.explanation}\n")
    for row in component_rows(c):
        print(f"  {row['Factor']:<20} {row['Value']:>3}  weight {row['Weight']:<5}  {row['Why']}")
    for heading, items in (("Strengths", c.strengths), ("Weaknesses", c.weaknesses)):
        if items:
            print(f"\n{heading}")
            for item in items:
                print(f"  - {item}")
    print("\nSignals")
    for s in signals_for(c, repo.signals(analysis_id)):
        row = signal_row(s)
        print(
            f"  [{row['Source']}] {row['Signal']:<18} {row['Start']} - {row['End']}  "
            f"{row['Detail']}"
        )
    print(f"\nExcerpt\n{c.excerpt}")
    return EXIT_OK


def cmd_transcript(settings: Settings, args: argparse.Namespace) -> int:
    """Print (or save under data/exports) the current transcript of a video."""
    with closing(open_database(settings.db_path)) as conn:
        video = VideoRepository(conn).resolve_reference(args.video)
        repo = TranscriptRepository(conn)
        transcript = repo.current(video.id)
        if transcript is None:
            print(
                f"Video #{video.id} has no transcript yet. Run `iftyedits transcribe {video.id}`.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        segments = repo.segments(transcript.id)
    text = export(args.format, transcript, segments)
    if args.save:
        name = f"{Path(video.original_filename).stem}-{transcript.model}.{args.format}"
        folder = settings.exports_dir / "transcripts" / video.sha256[:12]
        target = assert_safe_output_path(
            folder / name,
            originals_root=settings.originals_dir,
            allowed_roots=(settings.exports_dir,),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"Saved {target}")
        return EXIT_OK
    if args.format == "txt":
        for key, value in transcript_facts(transcript).items():
            print(f"# {key}: {value}", file=sys.stderr)
    sys.stdout.write(text)
    return EXIT_OK


def cmd_search(settings: Settings, args: argparse.Namespace) -> int:
    """Search the current transcripts of all (or one) videos."""
    with closing(open_database(settings.db_path)) as conn:
        video_id = None
        if args.video:
            video_id = VideoRepository(conn).resolve_reference(args.video).id
        hits = TranscriptRepository(conn).search(args.query, video_id=video_id, limit=args.limit)
    for hit in hits:
        row = search_row(hit)
        print(f"{row['Video']}  {row['Start']} - {row['End']}  {row['Match']}")
    if not hits:
        print("No matches.")
    return EXIT_OK


def cmd_worker(settings: Settings, args: argparse.Namespace) -> int:
    """Run the background worker until interrupted."""
    stop = threading.Event()

    def on_term(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    worker = Worker(settings)
    logger.info("Worker %s started (db %s)", worker.worker_id, settings.db_path)
    try:
        processed = worker.run(stop, once=args.once)
    except KeyboardInterrupt:
        logger.info("Worker stopping; any running job was returned to the queue")
        return EXIT_OK
    finally:
        worker.close()
    logger.info("Worker processed %d job(s)", processed)
    return EXIT_OK


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    """Print full details for one video, optionally re-verifying its vault hash."""
    with closing(open_database(settings.db_path)) as conn:
        repo = VideoRepository(conn)
        video = repo.resolve_reference(args.video)
        streams = repo.streams(video.id)
        sources = repo.sources(video.id)
    vault_path = settings.originals_dir / video.vault_relpath
    print(f"video       #{video.id}  {video.original_filename}")
    print(f"status      {video.status.value}")
    if video.probe_error:
        print(f"probe error {video.probe_error}")
    print(f"sha256      {video.sha256}")
    print(f"vault       {vault_path}")
    print(f"size        {human_size(video.size_bytes)} ({video.size_bytes} bytes)")
    print(f"duration    {human_duration(video.duration_s)}")
    print(f"container   {video.format_name or '-'}")
    print(f"imported    {video.imported_at}")
    print("streams")
    for stream in streams:
        row = stream_row(stream)
        print(
            f"  #{row['#']} {row['Type']:<8} {row['Codec']:<8} {row['Size']:<10} "
            f"fps {row['FPS']:<8} tb {stream.time_base or '-':<10} rot {row['Rotation']:<3} "
            f"{row['Audio']}  {row['Flags']}"
        )
    print(f"sources     {len(sources)}" + ("  (duplicate imports)" if len(sources) > 1 else ""))
    for source in sources:
        print(f"  {source.source_path}  ({source.imported_at})")
    if args.verify:
        intact = vault_path.is_file() and sha256_file(vault_path) == video.sha256
        print(f"integrity   {'OK' if intact else 'MISMATCH OR MISSING'}")
        return EXIT_OK if intact else EXIT_FAILED
    return EXIT_OK


def cmd_list(settings: Settings, args: argparse.Namespace) -> int:
    """Print imported videos."""
    with closing(open_database(settings.db_path)) as conn:
        videos = VideoRepository(conn).list_recent(args.limit)
    for v in videos:
        duration = f"{v.duration_s:8.1f}s" if v.duration_s is not None else "       -"
        print(f"#{v.id:<5} {v.sha256[:12]}  {duration}  {v.status.value:<12} {v.original_filename}")
    if not videos:
        print("No videos imported yet.")
    return EXIT_OK


def cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    """Adopt vault files missing from the database."""
    with closing(open_database(settings.db_path)) as conn:
        service = IngestService(
            settings,
            VideoRepository(conn),
            FFprobe(settings.ffprobe_path, settings.probe_timeout_s),
            build_vault(settings),
        )
        report = service.reconcile()
    print(f"Adopted {len(report.adopted)} file(s).")
    for sha in report.corrupt:
        print(f"CORRUPT  {sha}  (left untouched; investigate manually)", file=sys.stderr)
    return EXIT_FAILED if report.corrupt else EXIT_OK


def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    """Verify configuration, ffprobe, ffmpeg capabilities, the database and workers."""
    ok = True
    print(f"iftyedits   {__version__}")
    print(f"data dir    {settings.data_dir}")
    try:
        probe = FFprobe(settings.ffprobe_path, settings.probe_timeout_s)
        print(f"ffprobe     {probe.binary}")
    except IftyEditsError as exc:
        ok = False
        print(f"ffprobe     FAIL: {exc}")
    try:
        caps = FFmpeg(settings.ffmpeg_path, encoder=settings.proxy_encoder).capabilities()
    except IftyEditsError as exc:
        ok = False
        print(f"ffmpeg      FAIL: {exc}")
    else:
        print(f"ffmpeg      {caps.binary}  ({caps.version})")
        if caps.h264 is not None:
            kind = "hardware, verified" if caps.h264.hardware else "software"
            print(f"h264        {caps.h264.name} ({kind}; {caps.h264.reason})")
        for item in caps.missing:
            ok = False
            print(f"missing     {item}")
    version = engine_version()
    print(f"whisper     faster-whisper {version} (CPU)" if version else "whisper     NOT INSTALLED")
    ok = ok and version is not None
    store = build_model_store(settings)
    installed = [s.spec.name for s in store.statuses() if s.state is ModelState.INSTALLED]
    print(f"models      {store.root}  installed: {', '.join(installed) or 'none'}")
    if settings.transcription_model not in installed:
        print(
            f"note        default model {settings.transcription_model} is not downloaded "
            f"(`iftyedits models download {settings.transcription_model}`)"
        )
    print(f"clips       algorithmic scoring v{SCORING_VERSION} (no model, no network)")
    with closing(open_database(settings.db_path)) as conn:
        print(f"database    {settings.db_path} (schema v{schema_version(conn)})")
        workers = JobQueue(conn).live_workers()
    print(f"workers     {', '.join(workers) if workers else 'none running'}")
    return EXIT_OK if ok else EXIT_FAILED


def _connect_host(address: str) -> str:
    """The address to connect to for a server bound to ``address``."""
    return {"0.0.0.0": LOCALHOST, "::": "::1"}.get(address, address)  # noqa: S104 - lookup only


def port_in_use(address: str, port: int) -> bool:
    """True if something already listens on the port, or it cannot be bound."""
    try:
        with socket.create_connection((_connect_host(address), port), timeout=1.0):
            return True
    except OSError:
        pass
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError:
            return True
    return False


def dashboard_healthy(address: str, port: int, timeout_s: float = 2.0) -> bool:
    """True if a Streamlit server answers its health check on the port (no proxy is used)."""
    connection = http.client.HTTPConnection(_connect_host(address), port, timeout=timeout_s)
    try:
        connection.request("GET", HEALTH_PATH)
        response = connection.getresponse()
        return response.status == 200 and response.read(16).strip() == b"ok"
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _queue_state(settings: Settings) -> tuple[list[str], list[Job]] | None:
    """``(live workers, queued jobs)`` read without writing, or None if unreadable."""
    if not settings.db_path.is_file():
        return [], []
    try:
        with closing(open_read_only(settings.db_path)) as conn:
            queue = JobQueue(conn)
            queued = [j for j in queue.list_active() if j.status is JobStatus.QUEUED]
            return queue.live_workers(), queued
    except sqlite3.Error:
        logger.warning("Could not read the job database", exc_info=True)
        return None


def _describe_jobs(jobs: list[Job]) -> str:
    return ", ".join(f"#{j.id} {j.kind}" for j in sorted(jobs, key=lambda j: j.id))


def _spawn_worker(env: dict[str, str], home: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-m", "iftyedits.cli", "worker"], env=env, cwd=home)


def _stop_child(child: subprocess.Popen[bytes] | None, timeout_s: float) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        child.kill()


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt  # SIGTERM shuts the children down like Ctrl+C


def _wait_for_dashboard(server: subprocess.Popen[bytes], address: str, port: int) -> bool:
    """Wait until our dashboard answers. False if it exits or never becomes healthy."""
    deadline = time.monotonic() + DASHBOARD_START_TIMEOUT_S
    while server.poll() is None:
        if dashboard_healthy(address, port) and server.poll() is None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(DASHBOARD_POLL_S)
    return False


def _ui_port_taken(
    settings: Settings, args: argparse.Namespace, env: dict[str, str], home: Path
) -> int:
    """The port is occupied: never start a second dashboard, and a worker only on request."""
    url = f"http://{args.address}:{args.port}"
    if not dashboard_healthy(args.address, args.port):
        print(
            f"Port {args.port} on {args.address} cannot be used and no healthy dashboard "
            "answers there. Nothing was started. Free the port or choose one with --port.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    state = _queue_state(settings)
    if state is None:
        print(
            f"Dashboard already running at {url}. The job database could not be read, "
            "so nothing was started.",
            file=sys.stderr,
        )
        return EXIT_FAILED if args.start_worker else EXIT_OK
    workers, queued = state
    if workers:
        print(f"Dashboard and worker already running at {url}. Nothing was started.")
        return EXIT_OK
    if not args.start_worker:
        print(
            f"Dashboard already running at {url}. No worker is active, so "
            f"{len(queued)} queued job(s) will wait. Nothing was started."
        )
        if not args.no_worker:
            print("To process queued jobs, run `iftyedits ui --start-worker`.")
        return EXIT_OK
    print(f"Dashboard already running at {url}. Starting a worker only.")
    if queued:
        print(f"It will process {len(queued)} queued job(s): {_describe_jobs(queued)}")
    worker: subprocess.Popen[bytes] | None = None
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        worker = _spawn_worker(env, home)
        return worker.wait()
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        _stop_child(worker, 30)


def cmd_ui(settings: Settings, args: argparse.Namespace) -> int:
    """Start Streamlit on localhost, then a background worker unless disabled.

    The port is checked first, and the worker starts only after the dashboard is healthy, so a
    failed launch never claims a job. With a dashboard already running, nothing is started
    unless ``--start-worker`` is given and no worker is active.
    """
    home = project_home()
    app = home / "app" / "Home.py"
    if not app.is_file():
        print(f"Streamlit app not found at {app}. Run from the project root.", file=sys.stderr)
        return EXIT_USAGE
    env = {**os.environ, "IFTYEDITS_HOME": str(home)}
    if port_in_use(args.address, args.port):
        return _ui_port_taken(settings, args, env, home)
    ui_args = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address",
        args.address,
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    server: subprocess.Popen[bytes] | None = None
    worker: subprocess.Popen[bytes] | None = None
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        server = subprocess.Popen(ui_args, env=env, cwd=home)  # noqa: S603 - fixed argv
        if not args.no_worker:
            if not _wait_for_dashboard(server, args.address, args.port):
                print("The dashboard did not start, so no worker was started.", file=sys.stderr)
                return server.poll() or EXIT_FAILED
            state = _queue_state(settings)
            if state is not None and state[0]:
                print("A worker is already active; not starting another.")
            else:
                if state is not None and state[1]:
                    print(f"Starting a worker for queued job(s): {_describe_jobs(state[1])}")
                worker = _spawn_worker(env, home)
        return server.wait()
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        _stop_child(server, 20)
        _stop_child(worker, 30)


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for all commands."""
    parser = argparse.ArgumentParser(prog="iftyedits", description="IftyEdits AI Studio")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="import files or folders now (copies into the vault)")
    p.add_argument("paths", nargs="+")
    p.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("enqueue", help="queue files or folders for the background worker")
    p.add_argument("paths", nargs="+")
    p.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    p.add_argument(
        "--process",
        action="store_true",
        help="prepare media (audio, proxy, thumbnail) after each import",
    )
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("fetch", help="queue the download of a YouTube video you may use")
    p.add_argument("url", help="a youtube.com/watch, youtu.be or Shorts link")
    p.add_argument("--process", action="store_true", help="prepare media after the download")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser(
        "produce", help="queue the full pipeline: a link or stored video in, Shorts out"
    )
    p.add_argument("source", help="a YouTube link, a video id or a SHA-256 prefix")
    p.add_argument("--clips", type=int, default=5, help="most Shorts to make (default 5)")
    p.add_argument("--min-score", type=float, default=40.0, help="skip weaker suggestions")
    p.add_argument("--captions", choices=sorted(CAPTION_PRESETS), default="highlight")
    p.add_argument("--model", help="speech model (default from settings)")
    p.add_argument("--language", help="spoken language code (default: detect)")
    p.add_argument("--min-length", type=float, default=10.0, help="shortest Short in seconds")
    p.add_argument("--max-length", type=float, default=90.0, help="longest Short in seconds")
    p.add_argument("--review", action="store_true", help="choose clips but do not render them")
    p.add_argument("--force", action="store_true", help="redo stages even if stored")
    p.set_defaults(func=cmd_produce)

    p = sub.add_parser("worker", help="run the background job worker")
    p.add_argument("--once", action="store_true", help="exit when the queue is empty")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("process", help="queue media preparation for imported videos")
    p.add_argument("videos", nargs="*", help="video ids or SHA-256 prefixes")
    p.add_argument("--all", action="store_true", help="every imported video")
    p.add_argument(
        "--stage",
        action="append",
        choices=[s.value for s in ALL_STAGES],
        help="run only this stage (repeatable; default: all stages)",
    )
    p.add_argument(
        "--force", action="store_true", help="regenerate outputs even if valid ones exist"
    )
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("jobs", help="list background jobs")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--active", action="store_true", help="only queued and running jobs")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("cancel", help="cancel queued or running jobs")
    p.add_argument("job_ids", nargs="+", type=int, metavar="JOB_ID")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("artifacts", help="show processing stages and generated files of a video")
    p.add_argument("video", help="video id (e.g. 3) or SHA-256 prefix (at least 6 characters)")
    p.add_argument("--verify", action="store_true", help="re-hash every generated file")
    p.set_defaults(func=cmd_artifacts)

    p = sub.add_parser("analytics", help="performance of uploaded Shorts (read only)")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "refresh"])
    p.add_argument("video", nargs="?", help="YouTube link or video id (for add)")
    p.add_argument("--short", type=int, help="id of the Short the video came from (for add)")
    p.add_argument("--days", type=int, default=28, help="window for watch time (for refresh)")
    p.set_defaults(func=cmd_analytics)

    p = sub.add_parser("models", help="list, download or verify speech and face models")
    p.add_argument("action", nargs="?", default="list", choices=["list", "download", "verify"])
    p.add_argument("name", nargs="?", help="model name, e.g. small or yunet")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("transcribe", help="queue transcription of imported videos")
    p.add_argument("videos", nargs="*", help="video ids or SHA-256 prefixes")
    p.add_argument("--all", action="store_true", help="every imported video")
    p.add_argument("--model", help="speech model (default: IFTYEDITS_TRANSCRIPTION_MODEL)")
    p.add_argument("--language", help="language code such as en, or auto (default: setting)")
    p.add_argument("--force", action="store_true", help="transcribe again even if unchanged")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("analyze", help="queue clip analysis of transcribed videos")
    p.add_argument("videos", nargs="*", help="video ids or SHA-256 prefixes")
    p.add_argument("--all", action="store_true", help="every imported video")
    p.add_argument("--force", action="store_true", help="analyse again even if unchanged")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("clips", help="list, inspect, favorite or reject clip suggestions")
    p.add_argument("action", choices=["list", "show", "favorite", "reject", "clear"])
    p.add_argument("video", help="video id or SHA-256 prefix")
    p.add_argument("ref", nargs="?", help="suggestion rank or key prefix (not for list)")
    p.add_argument("--sort", choices=list(SORT_OPTIONS), default="rank")
    p.add_argument("--type", action="append", choices=list(CATEGORY_LABELS), help="moment type")
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--status", choices=list(DECISION_FILTERS), default="all")
    p.set_defaults(func=cmd_clips)

    p = sub.add_parser("transcript", help="print or save the current transcript of a video")
    p.add_argument("video", help="video id or SHA-256 prefix")
    p.add_argument("--format", choices=EXPORT_FORMATS, default="txt")
    p.add_argument("--save", action="store_true", help="write it under data/exports/transcripts")
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser("search", help="search transcripts")
    p.add_argument("query")
    p.add_argument("--video", help="only this video (id or SHA-256 prefix)")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="show details of one video")
    p.add_argument("video", help="video id (e.g. 3) or SHA-256 prefix (at least 6 characters)")
    p.add_argument("--verify", action="store_true", help="re-hash the vault copy")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list imported videos")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("reconcile", help="record vault files missing from the database")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser(
        "check", help="verify ffprobe, ffmpeg capabilities, data directory, database and workers"
    )
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("ui", help="start the Streamlit dashboard and a worker")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--address", default=LOCALHOST, help="bind address (default 127.0.0.1)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--no-worker", action="store_true", help="do not start a worker")
    mode.add_argument(
        "--start-worker",
        action="store_true",
        help="if the dashboard is already running and no worker is active, start a worker",
    )
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    _configure_logging(settings)
    try:
        return int(args.func(settings, args))
    except IftyEditsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
