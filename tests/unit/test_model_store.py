"""Speech-model store: status, verified downloads, failures and cleanup (no network)."""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import shutil
import threading
import time
import urllib.error
from collections import namedtuple
from pathlib import Path

import pytest

from iftyedits.adapters import model_store
from iftyedits.adapters.model_store import CATALOG, ModelStore, build_model_store, file_url
from iftyedits.config import Settings
from iftyedits.core.errors import (
    InsufficientSpaceError,
    JobCancelledError,
    ModelDownloadError,
    OriginalProtectionError,
    UnknownModelError,
)
from iftyedits.core.models import ModelFile, ModelState, SpeechModelSpec

WEIGHTS = b"W" * 3000
CONFIG = b"{}"
SPEC = SpeechModelSpec(
    name="mini",
    repo_id="Systran/faster-whisper-mini",
    revision="a" * 40,
    files=(
        ModelFile("config.json", len(CONFIG)),
        ModelFile("model.bin", len(WEIGHTS), hashlib.sha256(WEIGHTS).hexdigest()),
    ),
    summary="test",
    ram_gb=0.1,
)


class FakeResponse(io.BytesIO):
    """A response body; ``fail_after`` makes the connection drop after that many bytes."""

    def __init__(self, body: bytes, status: int, fail_after: int | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.fail_after = fail_after
        self.sent = 0

    def read(self, amount: int | None = -1, /) -> bytes:
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise http.client.IncompleteRead(b"")
        limit = amount if amount is not None and amount >= 0 else len(self.getbuffer())
        if self.fail_after is not None:
            limit = min(limit, self.fail_after - self.sent)
        data = super().read(limit)
        self.sent += len(data)
        return data


class FakeServer:
    """Serves files by URL (honouring Range offsets); records requests."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []
        self.offsets: list[int] = []
        self.error: Exception | None = None
        self.drops: list[int | None] = []  # connection drops after N bytes, one per request
        self.honour_range = True

    def __call__(self, url: str, timeout: float, offset: int = 0) -> FakeResponse:
        self.requested.append(url)
        self.offsets.append(offset)
        if self.error is not None:
            raise self.error
        body = self.files[url.rsplit("/", 1)[1]]
        resumed = offset > 0 and self.honour_range
        fail_after = self.drops.pop(0) if self.drops else None
        return FakeResponse(body[offset:] if resumed else body, 206 if resumed else 200, fail_after)


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("iftyedits.adapters.model_store.RETRY_DELAY_S", 0.0)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer({"config.json": CONFIG, "model.bin": WEIGHTS})


@pytest.fixture
def store(tmp_path: Path, server: FakeServer) -> ModelStore:
    return ModelStore(tmp_path / "models", catalog=(SPEC,), opener=server)


def test_catalog_is_pinned_and_consistent() -> None:
    names = [s.name for s in CATALOG]
    assert names == ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
    for spec in CATALOG:
        assert len(spec.revision) == 40 and spec.repo_id.count("/") == 1
        assert {"config.json", "model.bin", "tokenizer.json"} <= {f.name for f in spec.files}
        weights = next(f for f in spec.files if f.name == "model.bin")
        assert weights.sha256 is not None and len(weights.sha256) == 64
        assert file_url(spec, weights).startswith("https://huggingface.co/")
    sizes = [s.size_bytes for s in CATALOG]
    assert sizes[:4] == sorted(sizes[:4])


def test_status_and_unknown_model(store: ModelStore) -> None:
    status = store.status("mini")
    assert status.state is ModelState.MISSING and status.path == store.root / "mini"
    with pytest.raises(UnknownModelError, match="Supported models: mini"):
        store.status("huge")


def test_install_downloads_verifies_and_reports_progress(
    store: ModelStore, server: FakeServer
) -> None:
    seen: list[tuple[int, int]] = []
    status = store.install("mini", progress=lambda done, total: seen.append((done, total)))
    assert status.state is ModelState.INSTALLED
    assert (status.path / "model.bin").read_bytes() == WEIGHTS
    assert (status.path / model_store.MANIFEST).is_file()
    assert seen[-1] == (SPEC.size_bytes, SPEC.size_bytes)
    assert all(url.startswith("https://huggingface.co/Systran/") for url in server.requested)
    assert f"/resolve/{SPEC.revision}/" in server.requested[0]
    assert [p.name for p in store.root.iterdir()] == ["mini"]  # no staging left
    assert store.verify("mini")
    # Installing again does nothing.
    server.requested.clear()
    assert store.install("mini").state is ModelState.INSTALLED
    assert server.requested == []


def test_damaged_weights_fail_verification(store: ModelStore) -> None:
    store.install("mini")
    weights = store.root / "mini" / "model.bin"
    weights.write_bytes(b"X" * len(WEIGHTS))
    assert store.status("mini").state is ModelState.INSTALLED  # sizes still match
    assert not store.verify("mini")
    weights.write_bytes(b"short")
    status = store.status("mini")
    assert status.state is ModelState.INCOMPLETE and "wrong size" in status.detail


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"Y" * 3000, "SHA-256 check"),
        (b"W" * 10, "incomplete"),
        (b"W" * 4000, "larger than expected"),
    ],
)
def test_bad_downloads_are_discarded(
    store: ModelStore, server: FakeServer, body: bytes, message: str
) -> None:
    server.files["model.bin"] = body
    with pytest.raises(ModelDownloadError, match=message):
        store.install("mini")
    assert store.status("mini").state is ModelState.MISSING
    assert list(store.root.iterdir()) == []


def test_network_errors_are_explained(store: ModelStore, server: FakeServer) -> None:
    server.error = urllib.error.URLError("nodename nor servname provided")
    with pytest.raises(ModelDownloadError, match="Check the internet connection"):
        store.install("mini")
    server.error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    with pytest.raises(ModelDownloadError, match="HTTP 404"):
        store.install("mini")
    server.error = TimeoutError("timed out")
    with pytest.raises(ModelDownloadError, match="timed out"):
        store.install("mini")
    assert list(store.root.iterdir()) == []


def test_cancelled_download_leaves_nothing(store: ModelStore) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        store.install("mini", cancel=cancel)
    assert list(store.root.iterdir()) == []


def test_incomplete_install_is_replaced(store: ModelStore) -> None:
    folder = store.root / "mini"
    folder.mkdir(parents=True)
    (folder / "config.json").write_bytes(b"old")
    assert store.status("mini").state is ModelState.INCOMPLETE
    assert store.install("mini").state is ModelState.INSTALLED
    assert (folder / "config.json").read_bytes() == CONFIG


def test_insufficient_space_is_refused(store: ModelStore, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(10, 9, 1))
    with pytest.raises(InsufficientSpaceError, match="needs"):
        store.install("mini")


def test_unexpected_url_is_refused() -> None:
    with pytest.raises(ModelDownloadError, match="unexpected URL"):
        model_store._default_opener("http://example.com/model.bin", 1.0)


def test_models_dir_inside_vault_is_refused(tmp_path: Path, server: FakeServer) -> None:
    vault = tmp_path / "data" / "originals"
    store = ModelStore(vault / "models", catalog=(SPEC,), opener=server, forbidden_roots=(vault,))
    with pytest.raises(OriginalProtectionError):
        store.install("mini")
    assert not (vault / "models").exists()


def test_build_model_store_uses_settings(settings: Settings) -> None:
    store = build_model_store(settings)
    assert store.root == settings.models_dir / "faster-whisper"
    assert store.status("small").state is ModelState.MISSING


def test_clean_staging_removes_only_old_partial_downloads(store: ModelStore) -> None:
    store.root.mkdir(parents=True)
    old = store.root / f"{model_store.STAGING_PREFIX}mini-1234"
    new = store.root / f"{model_store.STAGING_PREFIX}mini-5678"
    keep = store.root / "mini"
    for folder in (old, new, keep):
        folder.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert store.clean_staging() == 1
    assert not old.exists() and new.exists() and keep.exists()


def test_remove_tree_refuses_outside_root(store: ModelStore, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(OriginalProtectionError):
        store._remove_tree(outside)
    with pytest.raises(OriginalProtectionError):
        store._remove_tree(store.root)
    assert outside.exists()


def test_dropped_connection_resumes_the_partial_file(store: ModelStore, server: FakeServer) -> None:
    server.drops = [None, 1200]  # config.json is fine; model.bin drops once mid-file
    assert store.install("mini").state is ModelState.INSTALLED
    assert (store.root / "mini" / "model.bin").read_bytes() == WEIGHTS
    assert server.offsets == [0, 0, 1200]
    assert list(store.root.iterdir()) == [store.root / "mini"]


def test_server_that_ignores_range_restarts_the_file(store: ModelStore, server: FakeServer) -> None:
    server.honour_range = False
    server.drops = [None, 1200]
    assert store.install("mini").state is ModelState.INSTALLED
    assert (store.root / "mini" / "model.bin").read_bytes() == WEIGHTS
    assert server.offsets == [0, 0, 1200]


def test_persistent_drops_give_up_and_install_nothing(
    store: ModelStore, server: FakeServer
) -> None:
    server.drops = [None] + [500] * 10
    with pytest.raises(ModelDownloadError, match="after 4 attempts"):
        store.install("mini")
    assert store.status("mini").state is ModelState.MISSING
    assert list(store.root.iterdir()) == []


def test_server_errors_are_retried_but_client_errors_are_not(
    store: ModelStore, server: FakeServer
) -> None:
    server.error = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)  # type: ignore[arg-type]
    with pytest.raises(ModelDownloadError, match="after 4 attempts"):
        store.install("mini")
    assert len(server.requested) == 4
    server.requested.clear()
    server.error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    with pytest.raises(ModelDownloadError, match="HTTP 404"):
        store.install("mini")
    assert len(server.requested) == 1


def test_partial_install_is_never_reported_installed(store: ModelStore, server: FakeServer) -> None:
    server.drops = [None] + [500] * 10
    with pytest.raises(ModelDownloadError):
        store.install("mini")
    assert not (store.root / "mini").exists()
    assert store.status("mini").state is ModelState.MISSING
