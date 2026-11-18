"""Domain error hierarchy for IftyEdits."""

from __future__ import annotations


class IftyEditsError(Exception):
    """Base class for all IftyEdits errors."""


class OriginalProtectionError(IftyEditsError):
    """An operation would modify, overwrite or write inside protected original media."""


class MediaIntegrityError(IftyEditsError):
    """A file's content does not match its expected SHA-256 hash."""


class InvalidSourceError(IftyEditsError):
    """A source path is missing, not a regular file or otherwise unusable."""


class InsufficientSpaceError(IftyEditsError):
    """Not enough free disk space to import a file safely."""


class ExternalToolError(IftyEditsError):
    """An external tool (ffprobe, ffmpeg) is missing, failed or timed out."""


class ProbeError(IftyEditsError):
    """A file could not be probed or its probe output could not be parsed."""


class UnsupportedMediaError(IftyEditsError):
    """A file is readable but is not a supported video."""


class ImportRecordError(IftyEditsError):
    """The original was stored in the vault but its database record could not be written.

    The vault copy is kept; ``reconcile`` adopts it later.
    """

    def __init__(self, message: str, sha256: str) -> None:
        super().__init__(message)
        self.sha256 = sha256
