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


class MediaValidationError(IftyEditsError):
    """A generated file does not match what the processing stage should have produced."""


class ProcessingError(IftyEditsError):
    """One or more media preparation stages failed. Details are on each stage record."""


class JobCancelledError(IftyEditsError):
    """Work stopped because the user cancelled the job. Any external process has exited."""


class JobOwnershipError(IftyEditsError):
    """A worker tried to update a job it no longer owns (for example after stale recovery)."""


class ImportRecordError(IftyEditsError):
    """The original was stored in the vault but its database record could not be written.

    The vault copy is kept; ``reconcile`` adopts it later.
    """

    def __init__(self, message: str, sha256: str) -> None:
        super().__init__(message)
        self.sha256 = sha256


class TranscriptionError(IftyEditsError):
    """A video could not be transcribed. The message says why and what to do."""


class NoAudioError(TranscriptionError):
    """The video has no audio stream, so there is nothing to transcribe."""


class TranscriptionEngineUnavailableError(TranscriptionError):
    """The speech-recognition engine (faster-whisper) is not installed or cannot load."""


class ModelNotInstalledError(TranscriptionError):
    """The requested speech model has not been downloaded yet."""


class UnknownModelError(TranscriptionError):
    """The requested speech model is not in the supported catalog."""


class TranscriptionTimeoutError(TranscriptionError):
    """Transcription took longer than the configured limit."""


class ModelDownloadError(IftyEditsError):
    """A speech model could not be downloaded or failed verification."""


class FaceDetectionError(IftyEditsError):
    """The face model could not be loaded or could not read a picture."""


class ClipAnalysisError(IftyEditsError):
    """Clip intelligence could not run for a video. The message says why and what to do."""


class ClipNotFoundError(ClipAnalysisError):
    """No clip candidate with the given id exists."""


class ShortConfigError(IftyEditsError):
    """A Short's edit settings are invalid (for example a reversed time range)."""


class ShortRenderError(IftyEditsError):
    """A Short could not be rendered or its output failed verification."""


class SourceDownloadError(IftyEditsError):
    """A video could not be fetched from a URL. The message says why and what to do."""


class LlmError(IftyEditsError):
    """The configured language model could not produce a usable answer."""


class LlmUnavailableError(LlmError):
    """No language model is configured, or the endpoint cannot be reached."""


class LlmRateLimitError(LlmError):
    """The language model endpoint asked us to slow down."""


class LlmBudgetError(LlmError):
    """A hard monthly Claude limit has been reached, so no further Claude call is made."""


class LlmTimeoutError(LlmError):
    """The language model did not answer in time."""


class LlmFormatError(LlmError):
    """The model answered, but not in the structured form the studio needs."""


class PublishError(IftyEditsError):
    """An upload-preparation action could not be completed (message says why)."""


class YouTubeError(IftyEditsError):
    """Base class for YouTube integration failures. ``kind`` is one of core.youtube.KIND_*."""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


class YouTubeNotConfiguredError(YouTubeError):
    """The OAuth client file is missing or the account is not connected."""


class YouTubeAuthError(YouTubeError):
    """Google rejected the credentials, or sign-in failed or was cancelled."""


class YouTubeQuotaError(YouTubeError):
    """The daily API quota or upload limit is used up. Retrying now will not help."""


class YouTubeUploadError(YouTubeError):
    """An upload failed for a reason other than authentication or quota."""


class UploadNotApprovedError(YouTubeError):
    """An upload was attempted without a valid, current approval of the exact details."""
