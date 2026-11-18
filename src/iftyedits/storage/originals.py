"""Original-media vault.

Guarantees, enforced here and covered by tests:

* Imports are independent byte copies. Sources are never moved, hard-linked or symlinked.
* The source is opened read-only and read once: copy and SHA-256 hash happen in one pass.
* Copies are written to a staging directory outside the vault, then moved into the
  vault under a lock, so the vault never contains partial files.
* Vault files are never overwritten. Identical content is stored once, whatever its
  filename or extension.
* Vault files are read-only (mode 0444).
* Imports are refused if they would leave less than ``reserve_bytes`` free.
* Generated output must pass :func:`assert_safe_output_path`, which rejects the vault.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from iftyedits.core.errors import (
    InsufficientSpaceError,
    InvalidSourceError,
    MediaIntegrityError,
    OriginalProtectionError,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
READ_ONLY_MODE = 0o444
PARTIAL_SUFFIX = ".partial"
STALE_PARTIAL_AGE_S = 3600.0
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VAULT_NAME_RE = re.compile(r"^(?P<sha>[0-9a-f]{64})(?P<suffix>\.[a-z0-9]{1,10})?$")
_NO_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})

ProgressCallback = Callable[[int, int], None]
"""Called with ``(bytes_copied, total_bytes)`` after each chunk."""

_progress: ContextVar[ProgressCallback | None] = ContextVar("vault_progress", default=None)


@dataclass(frozen=True, slots=True)
class ImportedOriginal:
    """Result of importing a source file into the vault."""

    sha256: str
    path: Path
    size_bytes: int
    source_path: Path
    already_present: bool


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    """Return True if ``path`` resolves to ``root`` or a location inside it."""
    return path.resolve().is_relative_to(root.resolve())


def assert_safe_output_path(
    path: Path,
    *,
    originals_root: Path,
    allowed_roots: Iterable[Path],
) -> Path:
    """Validate a path before any generated file is written to it.

    Returns the resolved path. Raises :class:`OriginalProtectionError` if the
    path is inside the originals vault (including via symlinks) or outside
    every allowed output root.
    """
    resolved = path.resolve()
    if is_within(resolved, originals_root):
        raise OriginalProtectionError(f"Refusing to write inside the originals vault: {resolved}")
    if not any(is_within(resolved, root) for root in allowed_roots):
        raise OriginalProtectionError(f"Output path is outside allowed roots: {resolved}")
    return resolved


def _normalise_suffix(suffix: str) -> str:
    lowered = suffix.lower()
    return lowered if _SUFFIX_RE.match(lowered) else ""


class OriginalsVault:
    """Content-addressed, read-only store of imported original media."""

    def __init__(
        self, root: Path, *, staging_dir: Path | None = None, reserve_bytes: int = 0
    ) -> None:
        self.root = root.resolve()
        self.staging_dir = (staging_dir or self.root.parent / "staging").resolve()
        if is_within(self.staging_dir, self.root) or is_within(self.root, self.staging_dir):
            raise OriginalProtectionError("Staging directory must be separate from the vault")
        self.reserve_bytes = reserve_bytes

    # ------------------------------------------------------------------ lookup

    def path_for(self, sha256: str, suffix: str) -> Path:
        """Return the vault location for a hash and file suffix."""
        if not _SHA256_RE.match(sha256):
            raise ValueError(f"Not a SHA-256 hex digest: {sha256!r}")
        return self.root / sha256[:2] / f"{sha256}{_normalise_suffix(suffix)}"

    def contains(self, path: Path) -> bool:
        """Return True if ``path`` resolves inside the vault."""
        return is_within(path, self.root)

    def verify(self, path: Path, expected_sha256: str) -> bool:
        """Return True if the file at ``path`` matches ``expected_sha256``."""
        return sha256_file(path) == expected_sha256

    def find_existing(self, sha256: str) -> Path | None:
        """Return the stored file for ``sha256`` under any extension, or None."""
        shard = self.path_for(sha256, "").parent
        if not shard.is_dir():
            return None
        for candidate in sorted(shard.iterdir()):
            match = _VAULT_NAME_RE.match(candidate.name)
            if match and match["sha"] == sha256:
                self._assert_regular_vault_file(candidate)
                return candidate
        return None

    def iter_originals(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(sha256, path)`` for every stored original."""
        if not self.root.is_dir():
            return
        for shard in sorted(self.root.iterdir()):
            if not shard.is_dir() or shard.is_symlink():
                continue
            for candidate in sorted(shard.iterdir()):
                match = _VAULT_NAME_RE.match(candidate.name)
                if match and match["sha"][:2] == shard.name and candidate.is_file():
                    yield match["sha"], candidate

    def relative_path(self, path: Path) -> Path:
        """Return ``path`` relative to the vault root."""
        return path.resolve().relative_to(self.root)

    # ------------------------------------------------------------------ import

    def import_file(
        self, source: Path, *, progress: ProgressCallback | None = None
    ) -> ImportedOriginal:
        """Copy ``source`` into the vault as an independent, read-only, verified file.

        The source is read once. Its size and modification time must be unchanged
        after the copy, otherwise the import is rejected and nothing is stored.
        """
        resolved = self._validate_source(source)
        before = resolved.stat()
        self._check_free_space(before.st_size)
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        if self.staging_dir.stat().st_dev != self.root.stat().st_dev:
            raise OriginalProtectionError("Staging and vault must be on the same filesystem")

        temp = self.staging_dir / f"{uuid.uuid4().hex}{PARTIAL_SUFFIX}"
        token = _progress.set(progress)
        try:
            copy_hash = self._exclusive_copy(resolved, temp)
        except OSError as exc:
            if exc.errno in _NO_SPACE_ERRNOS:
                raise InsufficientSpaceError(f"Disk full while importing {resolved.name}") from exc
            raise
        finally:
            _progress.reset(token)

        try:
            after = resolved.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise MediaIntegrityError(f"Source changed during import: {resolved}")
            temp.chmod(READ_ONLY_MODE)
            with self._lock():
                existing = self.find_existing(copy_hash)
                if existing is not None:
                    return self._existing(existing, copy_hash, before.st_size, resolved)
                dest = self.path_for(copy_hash, resolved.suffix)
                self._prepare_shard(dest.parent)
                temp.rename(dest)
        finally:
            if temp.exists():
                self._discard(temp)

        self._assert_independent(resolved, dest)
        logger.info("Imported original %s -> %s", resolved.name, dest.name)
        return ImportedOriginal(copy_hash, dest, before.st_size, resolved, already_present=False)

    def clean_staging(self, max_age_s: float = STALE_PARTIAL_AGE_S) -> int:
        """Delete abandoned partial copies left by interrupted imports.

        Only ``*.partial`` files in the staging directory are touched, never the
        vault. A file is removed only if it is older than ``max_age_s`` and no
        process holds its lock. Returns the number of files removed.
        """
        if not self.staging_dir.is_dir():
            return 0
        removed = 0
        cutoff = time.time() - max_age_s
        for partial in self.staging_dir.glob(f"*{PARTIAL_SUFFIX}"):
            if partial.is_symlink() or not partial.is_file() or partial.stat().st_mtime > cutoff:
                continue
            fd = os.open(partial, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            finally:
                os.close(fd)
            self._discard(partial)
            removed += 1
        if removed:
            logger.warning("Removed %d abandoned partial import(s)", removed)
        return removed

    # ------------------------------------------------------------------ internals

    def _validate_source(self, source: Path) -> Path:
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError as exc:
            raise InvalidSourceError(f"Source does not exist: {source}") from exc
        if not resolved.is_file():
            raise InvalidSourceError(f"Source is not a regular file: {source}")
        if self.contains(resolved):
            raise OriginalProtectionError(f"Source is already inside the vault: {resolved}")
        if is_within(resolved, self.staging_dir):
            raise OriginalProtectionError(f"Source is inside the staging area: {resolved}")
        return resolved

    def _check_free_space(self, size: int) -> None:
        probe_dir = self.root if self.root.exists() else self.root.parent
        while not probe_dir.exists():
            probe_dir = probe_dir.parent
        free = shutil.disk_usage(probe_dir).free
        if free - size < self.reserve_bytes:
            raise InsufficientSpaceError(
                f"Need {size} bytes plus {self.reserve_bytes} reserve, only {free} free"
            )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Exclusive lock on the vault directory for placement decisions."""
        fd = os.open(self.root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _prepare_shard(self, shard: Path) -> None:
        shard.mkdir(exist_ok=True)
        if shard.is_symlink() or shard.resolve().parent != self.root:
            raise OriginalProtectionError(f"Vault shard is not a real directory: {shard}")

    def _assert_regular_vault_file(self, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise OriginalProtectionError(f"Vault entry is not a regular file: {path}")

    def _existing(self, dest: Path, source_hash: str, size: int, source: Path) -> ImportedOriginal:
        if not self.verify(dest, source_hash):
            raise MediaIntegrityError(f"Vault file is corrupt and will not be replaced: {dest}")
        return ImportedOriginal(source_hash, dest, size, source, already_present=True)

    @staticmethod
    def _exclusive_copy(source: Path, dest: Path) -> str:
        """Stream-copy ``source`` to a newly created ``dest``; return the copy's hash.

        ``dest`` is created exclusively and locked while being written, so it can
        never replace an existing file and cleanup never removes an active copy.
        """
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        digest = hashlib.sha256()
        report = _progress.get()
        try:
            total = source.stat().st_size
            copied = 0
            with source.open("rb") as src, os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK_SIZE):
                    out.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    if report is not None:
                        report(copied, total)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            OriginalsVault._discard(dest)
            raise
        return digest.hexdigest()

    @staticmethod
    def _discard(path: Path) -> None:
        """Remove a staging copy that this vault itself created."""
        if not path.exists():
            return
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.unlink(missing_ok=True)

    @staticmethod
    def _assert_independent(source: Path, dest: Path) -> None:
        src_stat, dest_stat = source.stat(), dest.stat()
        if (src_stat.st_dev, src_stat.st_ino) == (dest_stat.st_dev, dest_stat.st_ino):
            raise OriginalProtectionError("Vault copy shares an inode with the source")
        if dest_stat.st_nlink != 1:
            raise OriginalProtectionError("Vault copy has extra hard links")
