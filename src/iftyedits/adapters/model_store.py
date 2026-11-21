"""Local store of faster-whisper (CTranslate2) speech models.

Models live in ``<models_dir>/faster-whisper/<name>/``. Nothing is downloaded unless
:meth:`ModelStore.install` is called (by an explicit ``download_model`` job or the
``iftyedits models download`` command). Transcription only ever loads installed models.

Downloads:

* come from Hugging Face over HTTPS at a pinned repository revision, so the same name
  always means the same weights;
* check free disk space first;
* stream into a hidden staging folder with byte-level progress and cancellation;
* verify every file's size, and the SHA-256 of the weights published by Hugging Face;
* replace the model folder only after every file has been verified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from iftyedits.config import Settings
from iftyedits.core.errors import (
    InsufficientSpaceError,
    JobCancelledError,
    ModelDownloadError,
    OriginalProtectionError,
    UnknownModelError,
)
from iftyedits.core.models import ModelFile, ModelState, ModelStatus, SpeechModelSpec
from iftyedits.core.protocols import ByteProgress, CancelToken
from iftyedits.storage.db import utc_now
from iftyedits.storage.originals import is_within, sha256_file

logger = logging.getLogger(__name__)

HF_BASE_URL = "https://huggingface.co"
ENGINE_DIR = "faster-whisper"
MANIFEST = "iftyedits-manifest.json"
STAGING_PREFIX = ".download-"
SPACE_MARGIN_BYTES = 512 * 1024**2
CHUNK_SIZE = 1024 * 1024
REQUEST_TIMEOUT_S = 60.0
STALE_STAGING_AGE_S = 3600.0
USER_AGENT = "iftyedits-model-store/1"


# Sizes and SHA-256 values were read from the Hugging Face API
# (``/api/models/<repo>?blobs=true``) on 2026-11-21 for the pinned revisions.
CATALOG: tuple[SpeechModelSpec, ...] = (
    SpeechModelSpec(
        name="tiny",
        repo_id="Systran/faster-whisper-tiny",
        revision="d90ca5fe260221311c53c58e660288d3deb8d356",
        files=(
            ModelFile("config.json", 2249),
            ModelFile("tokenizer.json", 2_203_239),
            ModelFile("vocabulary.txt", 459_861),
            ModelFile(
                "model.bin",
                75_538_270,
                "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
            ),
        ),
        summary="Fastest, least accurate. Good for quick drafts.",
        ram_gb=0.7,
    ),
    SpeechModelSpec(
        name="base",
        repo_id="Systran/faster-whisper-base",
        revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        files=(
            ModelFile("config.json", 2309),
            ModelFile("tokenizer.json", 2_203_239),
            ModelFile("vocabulary.txt", 459_861),
            ModelFile(
                "model.bin",
                145_217_532,
                "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
            ),
        ),
        summary="Fast, basic accuracy.",
        ram_gb=0.8,
    ),
    SpeechModelSpec(
        name="small",
        repo_id="Systran/faster-whisper-small",
        revision="536b0662742c02347bc0e980a01041f333bce120",
        files=(
            ModelFile("config.json", 2370),
            ModelFile("tokenizer.json", 2_203_239),
            ModelFile("vocabulary.txt", 459_861),
            ModelFile(
                "model.bin",
                483_546_902,
                "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
            ),
        ),
        summary="Balanced speed and accuracy. Recommended default.",
        ram_gb=1.0,
    ),
    SpeechModelSpec(
        name="medium",
        repo_id="Systran/faster-whisper-medium",
        revision="08e178d48790749d25932bbc082711ddcfdfbc4f",
        files=(
            ModelFile("config.json", 2257),
            ModelFile("tokenizer.json", 2_203_239),
            ModelFile("vocabulary.txt", 459_861),
            ModelFile(
                "model.bin",
                1_527_906_378,
                "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
            ),
        ),
        summary="More accurate, several times slower than small.",
        ram_gb=2.0,
    ),
    SpeechModelSpec(
        name="large-v3-turbo",
        repo_id="dropbox-dash/faster-whisper-large-v3-turbo",
        revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        files=(
            ModelFile("config.json", 2263),
            ModelFile("preprocessor_config.json", 340),
            ModelFile("tokenizer.json", 2_710_337),
            ModelFile("vocabulary.json", 1_068_114),
            ModelFile(
                "model.bin",
                1_617_884_929,
                "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
            ),
        ),
        summary="Near large-v3 accuracy, much faster than large-v3.",
        ram_gb=2.5,
    ),
    SpeechModelSpec(
        name="large-v3",
        repo_id="Systran/faster-whisper-large-v3",
        revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
        files=(
            ModelFile("config.json", 2394),
            ModelFile("preprocessor_config.json", 340),
            ModelFile("tokenizer.json", 2_480_617),
            ModelFile("vocabulary.json", 1_068_114),
            ModelFile(
                "model.bin",
                3_087_284_237,
                "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
            ),
        ),
        summary="Most accurate and slowest. Large download.",
        ram_gb=4.0,
    ),
)


def file_url(spec: SpeechModelSpec, file: ModelFile) -> str:
    """Download URL of one model file at the pinned revision."""
    return f"{HF_BASE_URL}/{spec.repo_id}/resolve/{spec.revision}/{file.name}"


class Response(Protocol):
    """The part of an HTTP response the store uses."""

    def read(self, amount: int = -1, /) -> bytes:
        """Read up to ``amount`` bytes."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


Opener = Callable[[str, float], Response]
"""Opens a URL with a timeout in seconds. Replaced in tests; never used for other hosts."""


def _default_opener(url: str, timeout: float) -> Response:
    if not url.startswith(f"{HF_BASE_URL}/"):
        raise ModelDownloadError(f"Refusing to download from an unexpected URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https only, checked above
    response: Response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    return response


class ModelStore:
    """Installed-model lookup and verified downloads. Implements ``SpeechModelStore``."""

    def __init__(
        self,
        models_dir: Path,
        *,
        catalog: tuple[SpeechModelSpec, ...] = CATALOG,
        opener: Opener | None = None,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        self.root = models_dir / ENGINE_DIR
        self._catalog = catalog
        self._open: Opener = opener if opener is not None else _default_opener
        self._forbidden = forbidden_roots

    # ------------------------------------------------------------------ queries

    def catalog(self) -> tuple[SpeechModelSpec, ...]:
        """Supported models, smallest first."""
        return self._catalog

    def spec(self, name: str) -> SpeechModelSpec:
        """One model's specification."""
        for spec in self._catalog:
            if spec.name == name:
                return spec
        names = ", ".join(s.name for s in self._catalog)
        raise UnknownModelError(f"Unknown speech model {name!r}. Supported models: {names}")

    def path(self, name: str) -> Path:
        """Folder an installed model lives in."""
        return self.root / self.spec(name).name

    def status(self, name: str) -> ModelStatus:
        """Installed, missing or incomplete (file missing or of the wrong size)."""
        spec = self.spec(name)
        folder = self.root / spec.name
        if not folder.exists():
            return ModelStatus(spec, ModelState.MISSING, folder, "Not downloaded")
        if folder.is_symlink() or not folder.is_dir():
            return ModelStatus(spec, ModelState.INCOMPLETE, folder, "Not a folder")
        problems: list[str] = []
        for file in spec.files:
            path = folder / file.name
            if not path.is_file():
                problems.append(f"{file.name} missing")
            elif path.stat().st_size != file.size_bytes:
                problems.append(f"{file.name} has the wrong size")
        if problems:
            return ModelStatus(spec, ModelState.INCOMPLETE, folder, "; ".join(problems))
        return ModelStatus(spec, ModelState.INSTALLED, folder, "Ready")

    def statuses(self) -> list[ModelStatus]:
        """Status of every catalog model."""
        return [self.status(spec.name) for spec in self._catalog]

    def verify(self, name: str) -> bool:
        """Re-hash an installed model's weights against the published SHA-256."""
        status = self.status(name)
        if status.state is not ModelState.INSTALLED:
            return False
        for file in status.spec.files:
            if file.sha256 is not None and sha256_file(status.path / file.name) != file.sha256:
                return False
        return True

    # ------------------------------------------------------------------ install

    def install(
        self,
        name: str,
        *,
        progress: ByteProgress | None = None,
        cancel: CancelToken | None = None,
    ) -> ModelStatus:
        """Download, verify and install a model (no-op if already installed)."""
        spec = self.spec(name)
        current = self.status(name)
        if current.state is ModelState.INSTALLED:
            if progress is not None:
                progress(spec.size_bytes, spec.size_bytes)
            return current
        self._check_location()
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_space(spec)
        staging = self.root / f"{STAGING_PREFIX}{spec.name}-{uuid.uuid4().hex[:8]}"
        staging.mkdir()
        try:
            done = 0
            for file in spec.files:
                done = self._download(spec, file, staging, done, progress, cancel)
            manifest = {
                "name": spec.name,
                "repo_id": spec.repo_id,
                "revision": spec.revision,
                "files": [asdict(f) for f in spec.files],
                "installed_at": utc_now(),
            }
            (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            final = self.root / spec.name
            if final.exists() or final.is_symlink():
                self._remove_tree(final)
            staging.rename(final)
        finally:
            if staging.exists():
                self._remove_tree(staging)
        installed = self.status(name)
        if installed.state is not ModelState.INSTALLED:
            raise ModelDownloadError(f"Model {name} failed verification after download")
        logger.info("Installed speech model %s from %s@%s", name, spec.repo_id, spec.revision)
        return installed

    def clean_staging(self, max_age_s: float = STALE_STAGING_AGE_S) -> int:
        """Remove abandoned download folders older than ``max_age_s``."""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - max_age_s
        removed = 0
        for entry in self.root.iterdir():
            if not entry.name.startswith(STAGING_PREFIX) or entry.is_symlink():
                continue
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                self._remove_tree(entry)
                removed += 1
        return removed

    # ------------------------------------------------------------------ helpers

    def _check_location(self) -> None:
        for root in self._forbidden:
            if is_within(self.root, root):
                raise OriginalProtectionError(
                    f"Models directory {self.root} is inside protected storage {root}"
                )

    def _check_space(self, spec: SpeechModelSpec) -> None:
        free = shutil.disk_usage(self.root).free
        needed = spec.size_bytes + SPACE_MARGIN_BYTES
        if free < needed:
            raise InsufficientSpaceError(
                f"Model {spec.name} needs {needed / 1024**3:.1f} GB free "
                f"(download plus margin); only {free / 1024**3:.1f} GB is free in {self.root}"
            )

    def _download(
        self,
        spec: SpeechModelSpec,
        file: ModelFile,
        staging: Path,
        done: int,
        progress: ByteProgress | None,
        cancel: CancelToken | None,
    ) -> int:
        target = staging / file.name
        digest = hashlib.sha256()
        written = 0
        url = file_url(spec, file)
        try:
            response = self._open(url, REQUEST_TIMEOUT_S)
            with closing(response), target.open("xb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise JobCancelledError(f"Download of model {spec.name} cancelled")
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > file.size_bytes:
                        raise ModelDownloadError(
                            f"{file.name} of model {spec.name} is larger than expected"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(done + written, spec.size_bytes)
        except urllib.error.HTTPError as exc:
            raise ModelDownloadError(
                f"Download of {file.name} for model {spec.name} failed: HTTP {exc.code} "
                f"{exc.reason}. Try again later."
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ModelDownloadError(
                f"Could not download model {spec.name} ({file.name}): {reason}. "
                "Check the internet connection and try again."
            ) from exc
        if written != file.size_bytes:
            raise ModelDownloadError(
                f"{file.name} of model {spec.name} is incomplete "
                f"({written} of {file.size_bytes} bytes). Try again."
            )
        if file.sha256 is not None and digest.hexdigest() != file.sha256:
            raise ModelDownloadError(
                f"{file.name} of model {spec.name} failed its SHA-256 check; it was discarded"
            )
        return done + written

    def _remove_tree(self, path: Path) -> None:
        """Delete a folder, but only inside this store's root."""
        if not is_within(path, self.root) or path.resolve() == self.root.resolve():
            raise OriginalProtectionError(f"Refusing to delete outside the model store: {path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def build_model_store(settings: Settings) -> ModelStore:
    """The model store for these settings; refuses a models directory inside the vault."""
    return ModelStore(settings.models_dir, forbidden_roots=(settings.originals_dir,))
