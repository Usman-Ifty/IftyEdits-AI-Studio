"""Original-media vault.

Guarantees, enforced here and covered by tests:

* Imports are independent byte copies. Sources are never moved, hard-linked or symlinked.
* Every original is identified by its SHA-256 hash; the copy is verified against it.
* Vault files are created with exclusive-create semantics and are never overwritten.
* Vault files are read-only (mode 0444).
* The source file is opened read-only and is left unchanged.
* Generated output must pass :func:`assert_safe_output_path`, which rejects the vault.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from iftyedits.core.errors import InvalidSourceError, MediaIntegrityError, OriginalProtectionError

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
READ_ONLY_MODE = 0o444
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

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

    def import_file(self, source: Path) -> ImportedOriginal:
        """Copy ``source`` into the vault as an independent, read-only, verified file."""
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError as exc:
            raise InvalidSourceError(f"Source does not exist: {source}") from exc
        if not resolved.is_file():
            raise InvalidSourceError(f"Source is not a regular file: {source}")
        if self.contains(resolved):
            raise OriginalProtectionError(f"Source is already inside the vault: {resolved}")

        before = resolved.stat()
        source_hash = sha256_file(resolved)
        dest = self.path_for(source_hash, resolved.suffix)

        if dest.exists():
            return self._existing(dest, source_hash, before.st_size, resolved)

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            copy_hash = self._exclusive_copy(resolved, dest)
        except FileExistsError:
            return self._existing(dest, source_hash, before.st_size, resolved)

        after = resolved.stat()
        if copy_hash != source_hash or (after.st_size, after.st_mtime_ns) != (
            before.st_size,
            before.st_mtime_ns,
        ):
            self._discard(dest)
            raise MediaIntegrityError(f"Source changed during import: {resolved}")

        dest.chmod(READ_ONLY_MODE)
        self._assert_independent(resolved, dest)
        logger.info("Imported original %s -> %s", resolved.name, dest.name)
        return ImportedOriginal(source_hash, dest, before.st_size, resolved, already_present=False)

    def _existing(self, dest: Path, source_hash: str, size: int, source: Path) -> ImportedOriginal:
        if not self.verify(dest, source_hash):
            raise MediaIntegrityError(f"Vault file is corrupt and will not be replaced: {dest}")
        return ImportedOriginal(source_hash, dest, size, source, already_present=True)

    @staticmethod
    def _exclusive_copy(source: Path, dest: Path) -> str:
        """Stream-copy ``source`` to a newly created ``dest``; return the copy's hash."""
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK_SIZE):
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            OriginalsVault._discard(dest)
            raise
        return digest.hexdigest()

    @staticmethod
    def _discard(dest: Path) -> None:
        """Remove a partial copy that this vault itself just created."""
        dest.chmod(stat.S_IWUSR | stat.S_IRUSR)
        dest.unlink(missing_ok=True)

    @staticmethod
    def _assert_independent(source: Path, dest: Path) -> None:
        src_stat, dest_stat = source.stat(), dest.stat()
        if (src_stat.st_dev, src_stat.st_ino) == (dest_stat.st_dev, dest_stat.st_ino):
            raise OriginalProtectionError("Vault copy shares an inode with the source")
        if dest_stat.st_nlink != 1:
            raise OriginalProtectionError("Vault copy has extra hard links")
