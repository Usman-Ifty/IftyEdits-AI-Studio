"""Phase 6.9: the real HTTP client against a local stub server, and end-to-end gaps.

The stub speaks the OpenAI chat-completions wire format over a real socket, so these tests
exercise ``urllib``, headers, status handling and timeouts. They do not exercise a real model.
"""

from __future__ import annotations

import http.server
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from iftyedits.adapters.llm import OpenAICompatibleModel
from iftyedits.config import Settings
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core.errors import (
    LlmError,
    LlmFormatError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from iftyedits.core.models import TranscriptSegment, Video
from iftyedits.core.shorts import ShortConfig
from iftyedits.services.ingest import IngestService
from iftyedits.services.publishing import PublishingService
from iftyedits.services.upload_package import build_package, write_package
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import KIND_EXPORT, ShortProject, ShortRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.conftest import MakeMedia

SECRET = "sk-test-SECRET-123"  # noqa: S105 - dummy value for leak checks
Reply = tuple[int, bytes, float]  # status, body, delay


class Stub:
    def __init__(self, reply: Callable[[dict[str, object]], Reply]) -> None:
        self.reply = reply
        self.requests: list[tuple[dict[str, str], dict[str, object]]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(length))
                stub.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                status, data, delay = stub.reply(body)
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def chat(content: object) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[dict[str, object]], Reply]], Stub]]:
    stubs: list[Stub] = []

    def start(reply: Callable[[dict[str, object]], Reply]) -> Stub:
        stub = Stub(reply)
        stubs.append(stub)
        return stub

    yield start
    for stub in stubs:
        stub.close()


def client(stub: Stub, timeout: float = 5.0) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(stub.url, "test-model", SECRET, timeout)


def test_success_sends_model_messages_and_bearer_key(serve: Callable[..., Stub]) -> None:
    stub = serve(lambda body: (200, chat("hello"), 0))
    assert client(stub).complete("sys", "usr") == "hello"
    headers, body = stub.requests[0]
    assert headers["authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "test-model" and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LlmUnavailableError),
        (403, LlmUnavailableError),
        (404, LlmUnavailableError),
        (429, LlmRateLimitError),
        (500, LlmError),
        (503, LlmError),
    ],
)
def test_http_errors_map_to_domain_errors_without_leaking_the_key(
    serve: Callable[..., Stub], status: int, error: type[Exception]
) -> None:
    stub = serve(lambda body: (status, b"{}", 0))
    with pytest.raises(error) as caught:
        client(stub).complete("s", "u")
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"{}", chat(""), chat("   "), chat(None), chat(["x"]), b'{"choices": []}'],
)
def test_malformed_or_empty_replies_raise_format_error(
    serve: Callable[..., Stub], payload: bytes
) -> None:
    stub = serve(lambda body: (200, payload, 0))
    with pytest.raises(LlmFormatError):
        client(stub).complete("s", "u")


def test_slow_server_times_out(serve: Callable[..., Stub]) -> None:
    stub = serve(lambda body: (200, chat("late"), 1.5))
    with pytest.raises(LlmTimeoutError):
        client(stub, timeout=0.3).complete("s", "u")


def test_refused_connection_is_unavailable_not_a_reply(serve: Callable[..., Stub]) -> None:
    stub = serve(lambda body: (200, chat("x"), 0))
    dead = OpenAICompatibleModel(stub.url, "m", SECRET, 2.0)
    stub.close()
    with pytest.raises(LlmUnavailableError) as caught:
        dead.complete("s", "u")
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "not a url", ""])
def test_non_http_endpoints_are_refused(url: str) -> None:
    with pytest.raises(LlmUnavailableError):
        OpenAICompatibleModel(url, "m", "", 1.0)


# ------------------------------------------------- the service through real HTTP

SPEECH = ["we met at the park", "he said you are a total idiot", "and then we left"]
CONFIG = ShortConfig(start_s=0.0, end_s=20.0)
META = {
    "titles": [{"text": "The park meeting", "style": "informative"}],
    "recommended_index": 0,
    "description": "A short scene at the park.",
    "hashtags": ["#park", "#park", "#shorts"],
    "tags": ["park", "Park", "meeting"],
    "summary": "Two people meet.",
    "confidence": "medium",
}


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Talk.mp4")).video


@pytest.fixture
def project(db_conn: sqlite3.Connection, video: Video) -> ShortProject:
    segs = [TranscriptSegment(i * 5.0, i * 5.0 + 4.0, t) for i, t in enumerate(SPEECH)]
    TranscriptRepository(db_conn).save(
        NewTranscript(
            video_id=video.id,
            audio_sha256="a" * 64,
            engine="fake",
            engine_version="1",
            model="m",
            model_revision=None,
            device="cpu",
            compute_type="int8",
            language_requested=None,
            language="en",
            language_probability=0.99,
            duration_s=20.0,
            params_hash="p" * 8,
            params={},
            segments=segs,
        )
    )
    return ShortRepository(db_conn).create_project(video.id, "k", "Park scene", 0.0, 20.0, CONFIG)


def export(settings: Settings, conn: sqlite3.Connection, video: Video, p: ShortProject) -> int:
    repo = ShortRepository(conn)
    digest = p.config.config_hash(video_sha256=video.sha256, kind=KIND_EXPORT, has_audio=True)
    render = repo.create_render(p.id, KIND_EXPORT, digest, p.config)
    repo.mark_running(render.id)
    out = settings.exports_dir / f"short-{render.id}.mp4"
    out.write_bytes(b"x" * 100)
    repo.mark_succeeded(
        render.id,
        output_path=out,
        width=1080,
        height=1920,
        duration_s=20.0,
        size_bytes=100,
        has_audio=True,
        encoder="libx264",
    )
    return render.id


def done(service: PublishingService, p: ShortProject, video: Video) -> None:
    service.edit_metadata(p.id, selected_title="The park meeting", description="A park scene.")
    service.set_setting(p.id, "audience", "not_for_kids")
    service.set_setting(p.id, "visibility", "private")
    for key in rd.USER_CHECKS:
        service.set_check(p.id, key, True)
    service.record_manual_review(p.id, "Watched, nothing concerning")


def test_fenced_json_reply_over_http_is_parsed_and_deduplicated(
    serve: Callable[..., Stub],
    settings: Settings,
    db_conn: sqlite3.Connection,
    project: ShortProject,
) -> None:
    fenced = "Here you go:\n```json\n" + json.dumps(META) + "\n```"
    stub = serve(lambda body: (200, chat(fenced), 0))
    service = PublishingService(settings, db_conn, client(stub))
    assert service.generate_metadata(project.id).ok
    stored = service.metadata(project.id)
    assert stored is not None
    assert stored.metadata.hashtags.count("#park") == 1
    assert len({t.lower() for t in stored.metadata.tags}) == len(stored.metadata.tags)
    user_prompt = str(stub.requests[0][1]["messages"][1]["content"])  # type: ignore[index]
    assert "we met at the park" in user_prompt and SECRET not in user_prompt


def test_provider_outage_is_a_failed_review_never_a_pass(
    serve: Callable[..., Stub],
    settings: Settings,
    db_conn: sqlite3.Connection,
    project: ShortProject,
) -> None:
    stub = serve(lambda body: (500, b"{}", 0))
    service = PublishingService(settings, db_conn, client(stub))
    service.run_review(project.id)
    assert service.facts(project.id).review_status not in ("completed", "manual")
    assert not service.metadata_is_current(project.id) or service.metadata(project.id) is None


def test_readiness_survives_closing_and_reopening_the_database(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = PublishingService(settings, db_conn, None)
    done(service, project, video)
    service.save_rights(
        video.id, rt.Rights(status=rt.ORIGINAL, source_title="Talk", creator="me"), confirmed=True
    )
    export(settings, db_conn, video, project)
    assert service.confirm_ready(project.id).status == rd.READY
    db_conn.close()
    reopened = open_database(settings.db_path)
    try:
        fresh = PublishingService(settings, reopened, None)
        assert fresh.readiness(project.id).status == rd.READY
        fresh.edit_metadata(project.id, description="changed")
        assert fresh.readiness(project.id).status != rd.READY
    finally:
        reopened.close()


def test_missing_attribution_blocks_confirmation(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = PublishingService(settings, db_conn, None)
    done(service, project, video)
    export(settings, db_conn, video, project)
    service.save_rights(
        video.id,
        rt.Rights(
            status=rt.LICENSE,
            license_type="CC-BY",
            license_url="https://example.test/l",
            attribution_required="yes",
        ),
        confirmed=True,
    )
    assert not service.readiness(project.id).can_confirm


def test_package_matches_export_hides_secrets_and_paths(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = PublishingService(settings, db_conn, None)
    done(service, project, video)
    service.edit_metadata(project.id, selected_title="../../etc/passwd: a/b\\c <x>")
    export_id = export(settings, db_conn, video, project)
    path = write_package(service, project.id, "json")
    assert path.parent == settings.exports_dir / "metadata"
    assert path.resolve().is_relative_to(settings.exports_dir.resolve())
    assert ".." not in path.name and "/" not in path.name and "\\" not in path.name
    text = path.read_text()
    assert SECRET not in text and str(settings.data_dir) not in text
    data = json.loads(text)
    assert data["short"]["export_render_id"] == export_id
    assert data["short"]["export_file"] == f"short-{export_id}.mp4"
    assert build_package(service, project.id)["title"] == data["title"]
    assert list(path.parent.iterdir()) == [path]


def test_restricted_source_cannot_be_confirmed_through_the_service(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = PublishingService(settings, db_conn, None)
    done(service, project, video)
    export(settings, db_conn, video, project)
    service.save_rights(video.id, rt.Rights(status=rt.RESTRICTED), confirmed=True)
    from iftyedits.core.errors import PublishError

    with pytest.raises(PublishError):
        service.confirm_ready(project.id)


def test_stub_files_stay_in_tmp(tmp_path: Path, settings: Settings) -> None:
    assert settings.data_dir.is_relative_to(tmp_path)
