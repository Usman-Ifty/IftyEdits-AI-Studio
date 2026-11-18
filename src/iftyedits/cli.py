"""Command-line interface: ``iftyedits <command>``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from types import FrameType

from iftyedits import __version__
from iftyedits.adapters.ffprobe import FFprobe
from iftyedits.config import Settings, project_home
from iftyedits.core.errors import IftyEditsError
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.pipeline.worker import Worker
from iftyedits.services.ingest import IngestService, build_vault, discover, validate_source
from iftyedits.storage.db import open_database, schema_version
from iftyedits.storage.originals import ProgressCallback, sha256_file
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui.formatting import human_duration, human_size, stream_row

logger = logging.getLogger("iftyedits")

LOCALHOST = "127.0.0.1"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


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
        ids = JobQueue(conn).enqueue_imports(files)
    print(f"Queued {len(ids)} job(s); {len(files) - len(ids)} already queued.")
    return EXIT_OK if files else EXIT_FAILED


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
    """Verify configuration, ffprobe and the database."""
    ok = True
    print(f"iftyedits   {__version__}")
    print(f"data dir    {settings.data_dir}")
    try:
        probe = FFprobe(settings.ffprobe_path, settings.probe_timeout_s)
        print(f"ffprobe     {probe.binary}")
    except IftyEditsError as exc:
        ok = False
        print(f"ffprobe     FAIL: {exc}")
    with closing(open_database(settings.db_path)) as conn:
        print(f"database    {settings.db_path} (schema v{schema_version(conn)})")
        workers = JobQueue(conn).live_workers()
    print(f"workers     {', '.join(workers) if workers else 'none running'}")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_ui(settings: Settings, args: argparse.Namespace) -> int:
    """Start Streamlit on localhost, plus a background worker unless disabled."""
    home = project_home()
    app = home / "app" / "Home.py"
    if not app.is_file():
        print(f"Streamlit app not found at {app}. Run from the project root.", file=sys.stderr)
        return EXIT_USAGE
    env = {**os.environ, "IFTYEDITS_HOME": str(home)}
    worker: subprocess.Popen[bytes] | None = None
    if not args.no_worker:
        worker = subprocess.Popen(
            [sys.executable, "-m", "iftyedits.cli", "worker"], env=env, cwd=home
        )
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
    try:
        return subprocess.call(ui_args, env=env, cwd=home)  # noqa: S603 - fixed argv
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                worker.kill()


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
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("worker", help="run the background job worker")
    p.add_argument("--once", action="store_true", help="exit when the queue is empty")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("show", help="show details of one video")
    p.add_argument("video", help="video id (e.g. 3) or SHA-256 prefix (at least 6 characters)")
    p.add_argument("--verify", action="store_true", help="re-hash the vault copy")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list imported videos")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("reconcile", help="record vault files missing from the database")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("check", help="verify ffprobe, data directory and database")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("ui", help="start the Streamlit dashboard and a worker")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--address", default=LOCALHOST, help="bind address (default 127.0.0.1)")
    p.add_argument("--no-worker", action="store_true", help="do not start a worker")
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
