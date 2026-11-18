"""Video import: validate, discover, copy into the vault, probe and record.

Order of operations for one file:

1. Validate the path (regular file, allowed extension, outside the data directory).
2. Probe the source read-only and reject anything without a video stream, so
   non-video files are never copied.
3. If stored content has the same size, hash the source; identical content is
   recorded as another source of the existing video and is not copied again.
4. Copy into the vault (single pass, hash verified, read-only).
5. Probe the vault copy and record everything in one database transaction.

If step 5 cannot write to the database the vault copy is kept and
:meth:`IngestService.reconcile` adopts it later. Originals are never deleted.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core.errors import (
    ExternalToolError,
    ImportRecordError,
    InvalidSourceError,
    OriginalProtectionError,
    ProbeError,
    UnsupportedMediaError,
)
from iftyedits.core.models import ImportOutcome, ImportResult, MediaInfo, Video
from iftyedits.core.protocols import MediaProber
from iftyedits.storage.originals import (
    OriginalsVault,
    ProgressCallback,
    is_within,
    sha256_file,
)
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Discovery:
    """Video files found in a folder, plus entries skipped for safety reasons."""

    files: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...] = ()


@dataclass(slots=True)
class ReconcileReport:
    """Outcome of adopting vault files that have no database record."""

    adopted: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)


def build_vault(settings: Settings) -> OriginalsVault:
    """Vault configured from settings."""
    return OriginalsVault(
        settings.originals_dir,
        staging_dir=settings.staging_dir,
        reserve_bytes=settings.min_free_bytes,
    )


def validate_source(settings: Settings, source: Path) -> Path:
    """Resolve and check a user-supplied file path. Symlinks resolve to their target."""
    try:
        resolved = source.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise InvalidSourceError(f"File not found: {source}") from exc
    if not resolved.is_file():
        raise InvalidSourceError(f"Not a regular file: {source}")
    if is_within(resolved, settings.data_dir):
        raise OriginalProtectionError(
            f"Refusing to import from the application data directory: {resolved}"
        )
    if resolved.suffix.lower() not in settings.video_extensions:
        raise UnsupportedMediaError(f"Unsupported file extension: {resolved.name}")
    return resolved


def discover(settings: Settings, folder: Path, *, recursive: bool = False) -> Discovery:
    """List importable videos in ``folder``.

    Symbolic links (files and directories) are never followed and are reported
    as skipped. Hidden files and files with other extensions are ignored.
    """
    try:
        root = folder.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise InvalidSourceError(f"Folder not found: {folder}") from exc
    if not root.is_dir():
        raise InvalidSourceError(f"Not a folder: {folder}")
    if is_within(root, settings.data_dir) or is_within(settings.data_dir, root):
        raise OriginalProtectionError(f"Folder overlaps the application data directory: {root}")

    files: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in sorted(dirnames):
            if (current / name).is_symlink():
                skipped.append((current / name, "symbolic link to a folder"))
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) if recursive else []
        for name in sorted(filenames):
            path = current / name
            if name.startswith("."):
                continue
            if path.suffix.lower() not in settings.video_extensions:
                continue
            if path.is_symlink():
                skipped.append((path, "symbolic link"))
                continue
            if path.is_file():
                files.append(path)
    return Discovery(tuple(files), tuple(skipped))


class IngestService:
    """Imports local video files into the protected vault and the database."""

    def __init__(
        self,
        settings: Settings,
        repo: VideoRepository,
        prober: MediaProber,
        vault: OriginalsVault | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.prober = prober
        self.vault = vault or build_vault(settings)

    def validate_source(self, source: Path) -> Path:
        """See :func:`validate_source`."""
        return validate_source(self.settings, source)

    def discover(self, folder: Path, *, recursive: bool = False) -> Discovery:
        """See :func:`discover`."""
        return discover(self.settings, folder, recursive=recursive)

    # ------------------------------------------------------------------ import

    def import_video(
        self, source: Path, *, progress: ProgressCallback | None = None
    ) -> ImportResult:
        """Import one file. Returns the stored video and whether it was new."""
        resolved = self.validate_source(source)
        self._require_video(resolved)

        duplicate = self._find_duplicate(resolved)
        if duplicate is not None:
            self.repo.add_source(duplicate.id, resolved)
            logger.info("Duplicate content %s already stored", duplicate.sha256[:12])
            return ImportResult(duplicate, ImportOutcome.DUPLICATE, resolved)

        stored = self.vault.import_file(resolved, progress=progress)
        info, probe_error = self._probe_stored(stored.path)
        try:
            video, created = self.repo.record_import(
                sha256=stored.sha256,
                vault_relpath=str(self.vault.relative_path(stored.path)),
                original_filename=resolved.name,
                size_bytes=stored.size_bytes,
                info=info,
                probe_error=probe_error,
                source_path=resolved,
            )
        except sqlite3.Error as exc:
            logger.error("Database write failed; original kept in vault: %s", stored.path)
            raise ImportRecordError(
                f"Stored {resolved.name} in the vault but could not record it ({exc}). "
                "The original is safe; run `iftyedits reconcile` to recover the record.",
                stored.sha256,
            ) from exc
        outcome = ImportOutcome.IMPORTED if created else ImportOutcome.DUPLICATE
        return ImportResult(video, outcome, resolved)

    def reconcile(self) -> ReconcileReport:
        """Record vault files that have no database row (e.g. after a DB failure).

        Files are verified against their hash first. Corrupt files are reported
        and left untouched.
        """
        report = ReconcileReport()
        known = self.repo.known_hashes()
        for sha, path in self.vault.iter_originals():
            if sha in known:
                continue
            if not self.vault.verify(path, sha):
                logger.error("Vault file does not match its hash: %s", path)
                report.corrupt.append(sha)
                continue
            info, probe_error = self._probe_stored(path)
            self.repo.record_import(
                sha256=sha,
                vault_relpath=str(self.vault.relative_path(path)),
                original_filename=path.name,
                size_bytes=path.stat().st_size,
                info=info,
                probe_error=probe_error,
                source_path=None,
            )
            report.adopted.append(sha)
        return report

    # ------------------------------------------------------------------ helpers

    def _require_video(self, path: Path) -> MediaInfo:
        try:
            info = self.prober.probe(path)
        except ProbeError as exc:
            raise UnsupportedMediaError(f"Not a readable media file: {path.name}") from exc
        if info.primary_video is None:
            raise UnsupportedMediaError(f"No video stream found in {path.name}")
        return info

    def _find_duplicate(self, path: Path) -> Video | None:
        candidates = self.repo.find_by_size(path.stat().st_size)
        if not candidates:
            return None
        return self.repo.get_by_sha(sha256_file(path))

    def _probe_stored(self, path: Path) -> tuple[MediaInfo | None, str | None]:
        try:
            return self.prober.probe(path), None
        except (ProbeError, ExternalToolError) as exc:
            logger.warning("Probe of stored original failed: %s", exc)
            return None, str(exc)
