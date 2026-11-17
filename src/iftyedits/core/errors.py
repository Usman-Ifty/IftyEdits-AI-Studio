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
