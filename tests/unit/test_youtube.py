# ruff: noqa: F811
"""Phase 7: optional YouTube upload. A local stub speaks Google's wire formats; no real calls."""

from __future__ import annotations

import http.server
import json
import os
import sqlite3
import stat
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from iftyedits.adapters.youtube import ClientConfig, TokenStore, YouTubeClient, load_client_config
from iftyedits.config import Settings
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import youtube as yt
from iftyedits.core.errors import (
    JobCancelledError,
    PublishError,
    UploadNotApprovedError,
    YouTubeAuthError,
    YouTubeNotConfiguredError,
    YouTubeQuotaError,
    YouTubeUploadError,
)
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import UPLOAD_YOUTUBE
from iftyedits.services.publishing import PublishingService
from iftyedits.services.youtube_upload import YouTubeUploadService
from iftyedits.storage.shorts import ShortProject
from tests.unit.test_phase69_validation import done, export, project, video  # noqa: F401

SECRET = "ya29.SECRET-ACCESS-TOKEN"  # noqa: S105 - dummy values for leak checks
REFRESH = "1//SECRET-REFRESH-TOKEN"


class Google:
    """Stub of the token, channels and resumable-upload endpoints."""

    def __init__(self) -> None:
        self.channel: dict[str, object] | None = {"id": "UC123", "snippet": {"title": "My Channel"}}
        self.start_status = 200
        self.start_body = b"{}"
        self.put_script: list[int] = []  # statuses to answer before behaving normally
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.received = 0
        self.total = 0
        self.token_status = 200
        google = self

        class H(http.server.BaseHTTPRequestHandler):
            def _read(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _send(
                self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                body = self._read()
                head = {k.lower(): v for k, v in self.headers.items()}
                google.requests.append(("POST", self.path, head, body))
                if self.path.startswith("/token"):
                    if google.token_status != 200:
                        return self._send(google.token_status, b"{}")
                    form = urllib.parse.parse_qs(body.decode())
                    out = {
                        "access_token": SECRET,
                        "expires_in": 3600,
                        "scope": " ".join(yt.SCOPES),
                    }
                    if form.get("grant_type") == ["authorization_code"]:
                        out["refresh_token"] = REFRESH
                    return self._send(200, json.dumps(out).encode())
                if self.path.startswith("/upload/youtube/v3/videos"):
                    if google.start_status != 200:
                        return self._send(google.start_status, google.start_body)
                    google.total = int(head["x-upload-content-length"])
                    google.received = 0
                    loc = f"http://127.0.0.1:{google.port}/session/1"
                    return self._send(200, b"{}", {"Location": loc})
                self._send(404)

            def do_GET(self) -> None:
                head = {k.lower(): v for k, v in self.headers.items()}
                google.requests.append(("GET", self.path, head, b""))
                if self.path.startswith("/youtube/v3/channels"):
                    items = [google.channel] if google.channel else []
                    return self._send(200, json.dumps({"items": items}).encode())
                self._send(404)

            def do_PUT(self) -> None:
                body = self._read()
                head = {k.lower(): v for k, v in self.headers.items()}
                google.requests.append(("PUT", self.path, head, b"" if not body else b"<chunk>"))
                if google.put_script:
                    status = google.put_script.pop(0)
                    if status != 0:
                        return self._send(status, b"{}")
                rng = head["content-range"]
                if rng.startswith("bytes */"):
                    if google.received == 0:
                        return self._send(308)
                    return self._send(308, headers={"Range": f"bytes=0-{google.received - 1}"})
                span = rng.split(" ")[1].split("/")[0]
                start = int(span.split("-")[0])
                if start != google.received:
                    return self._send(400, b"{}")
                google.received += len(body)
                if google.received >= google.total:
                    return self._send(200, json.dumps({"id": "VID_abc123"}).encode())
                self._send(308, headers={"Range": f"bytes=0-{google.received - 1}"})

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for m, p, _, _ in self.requests if m == method and p.startswith(prefix))


@pytest.fixture
def google() -> Iterator[Google]:
    g = Google()
    yield g
    g.close()


def make_client(
    g: Google, tmp_path: Path, *, signed_in: bool = True, chunk: int = 256 * 1024
) -> YouTubeClient:
    store = TokenStore(tmp_path / "cfg" / "token.json")
    if signed_in:
        store.save({"access_token": "old", "refresh_token": REFRESH, "expires_at": 0, "scope": ""})
    cfg = ClientConfig("cid", "csecret", f"{g.base}/auth", f"{g.base}/token")
    return YouTubeClient(
        cfg,
        store,
        chunk_bytes=chunk,
        api_base=f"{g.base}/youtube/v3",
        upload_base=f"{g.base}/upload/youtube/v3",
        sleep=lambda s: None,
    )


# ------------------------------------------------------------------ core


def details(**over: object) -> yt.UploadDetails:
    base: dict[str, object] = {
        "project_id": 1,
        "render_id": 1,
        "file_name": "a.mp4",
        "file_size": 10,
        "file_sha256": "a" * 64,
        "title": "A title",
        "description": "Desc",
        "tags": ("x",),
        "visibility": "private",
        "made_for_kids": False,
        "channel_id": "UC1",
        "channel_title": "Chan",
    }
    base.update(over)
    return yt.UploadDetails(**base)  # type: ignore[arg-type]


def test_valid_details_have_no_problems() -> None:
    assert yt.validate(details()) == []


@pytest.mark.parametrize(
    "over",
    [
        {"visibility": "public"},
        {"visibility": ""},
        {"title": ""},
        {"title": "x" * 101},
        {"title": "a<b"},
        {"description": "é" * 3000},
        {"description": "a>b"},
        {"tags": tuple("t" * 30 for _ in range(30))},
        {"file_name": "a.mov"},
        {"channel_id": ""},
        {"file_size": 0},
    ],
)
def test_invalid_details_are_refused(over: dict[str, object]) -> None:
    assert yt.validate(details(**over))


def test_public_is_never_allowed_in_the_request_body() -> None:
    assert "public" not in yt.ALLOWED_VISIBILITY
    body = yt.video_resource(details(visibility="unlisted", made_for_kids=True))
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True}


def test_fingerprint_changes_with_every_field() -> None:
    base = details().fingerprint()
    for over in (
        {"title": "B"},
        {"description": "Other"},
        {"tags": ("y",)},
        {"visibility": "unlisted"},
        {"channel_id": "UC2"},
        {"file_sha256": "b" * 64},
        {"made_for_kids": True},
    ):
        assert details(**over).fingerprint() != base


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (403, b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}', yt.KIND_QUOTA),
        (403, b'{"error":{"errors":[{"reason":"uploadLimitExceeded"}]}}', yt.KIND_QUOTA),
        (429, b"{}", yt.KIND_RATE),
        (401, b"{}", yt.KIND_AUTH),
        (401, b'{"error":{"errors":[{"reason":"authError"}]}}', yt.KIND_AUTH),
        (403, b'{"error":{"errors":[{"reason":"youtubeSignupRequired"}]}}', yt.KIND_NO_CHANNEL),
        (403, b"{}", yt.KIND_REJECTED),
        (400, b'{"error":{"message":"bad"}}', yt.KIND_REJECTED),
        (503, b"x", yt.KIND_NETWORK),
    ],
)
def test_api_errors_are_classified(status: int, body: bytes, kind: str) -> None:
    assert yt.classify_api_error(status, body)[0] == kind


# ------------------------------------------------------------------ token store + client file


def test_token_file_is_private_atomic_and_tightened(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "cfg" / "t.json")
    store.save({"refresh_token": REFRESH, "access_token": "a"})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    store.path.chmod(0o644)
    assert store.load() is not None
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert [p.name for p in store.path.parent.iterdir()] == ["t.json"]
    store.delete()
    assert store.load() is None


def test_token_file_inside_the_project_is_refused(tmp_path: Path) -> None:
    with pytest.raises(YouTubeNotConfiguredError):
        TokenStore(tmp_path / "repo" / "tok.json", forbidden_root=tmp_path / "repo")


def test_corrupt_token_file_reads_as_not_connected(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.json")
    store.path.write_text("not json")
    assert store.load() is None


def test_client_file_must_be_a_desktop_client(tmp_path: Path) -> None:
    with pytest.raises(YouTubeNotConfiguredError):
        load_client_config(None)
    with pytest.raises(YouTubeNotConfiguredError):
        load_client_config(tmp_path / "missing.json")
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(YouTubeNotConfiguredError, match="Desktop"):
        load_client_config(web)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    assert load_client_config(ok).token_uri.startswith("https://oauth2.googleapis.com")


# ------------------------------------------------------------------ client against the stub


def test_expired_token_is_refreshed_and_saved(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path)
    assert client.access_token() == SECRET
    assert google.count("POST", "/token") == 1
    assert client.access_token() == SECRET  # now cached, no second refresh
    assert google.count("POST", "/token") == 1
    assert client.store.load()["refresh_token"] == REFRESH  # type: ignore[index]


def test_refresh_failure_asks_to_reconnect_without_leaking(google: Google, tmp_path: Path) -> None:
    google.token_status = 400
    with pytest.raises(YouTubeAuthError, match="again") as caught:
        make_client(google, tmp_path).access_token()
    assert REFRESH not in str(caught.value) and SECRET not in str(caught.value)


def test_no_token_means_not_connected(google: Google, tmp_path: Path) -> None:
    with pytest.raises(YouTubeNotConfiguredError):
        make_client(google, tmp_path, signed_in=False).access_token()


def test_channel_lookup_and_missing_channel(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path)
    assert client.channel() == ("UC123", "My Channel")
    google.channel = None
    with pytest.raises(YouTubeUploadError, match="no YouTube channel"):
        client.channel()


def test_browser_sign_in_with_pkce_and_state(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path, signed_in=False)
    seen: dict[str, str] = {}

    def fake_browser(url: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen.update({k: v[0] for k, v in q.items()})
        redirect = q["redirect_uri"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(  # noqa: S310
                f"{redirect}/?code=AUTHCODE&state={q['state'][0]}", timeout=5
            ).read(),
            daemon=True,
        ).start()

    client._open = fake_browser
    client.authorize(wait_s=10)
    assert seen["code_challenge_method"] == "S256" and seen["redirect_uri"].startswith(
        "http://127.0.0.1:"
    )
    assert "youtube.upload" in seen["scope"] and seen["access_type"] == "offline"
    form = urllib.parse.parse_qs(
        next(b for m, p, _, b in google.requests if p == "/token").decode()
    )
    assert form["grant_type"] == ["authorization_code"] and "code_verifier" in form
    assert client.connected()


def test_sign_in_with_wrong_state_is_rejected(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path, signed_in=False)

    def evil(url: str) -> None:
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(  # noqa: S310
                f"{redirect}/?code=X&state=WRONG", timeout=5
            ).read(),
            daemon=True,
        ).start()

    client._open = evil
    with pytest.raises(YouTubeAuthError, match="did not match"):
        client.authorize(wait_s=10)
    assert not client.connected()


def test_sign_in_timeout(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path, signed_in=False)
    client._open = lambda url: None
    with pytest.raises(YouTubeAuthError, match="not completed"):
        client.authorize(wait_s=0.3)


def big_file(tmp_path: Path, size: int = 700 * 1024) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(os.urandom(size))
    return path


def test_chunked_resumable_upload_reports_progress(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path)
    file = big_file(tmp_path)
    seen: list[tuple[int, int]] = []
    vid = client.upload(
        file, yt.video_resource(details()), on_progress=lambda a, b: seen.append((a, b))
    )
    assert vid == "VID_abc123"
    assert google.count("PUT", "/session") == 3  # 700 KiB in 256 KiB chunks
    assert seen[-1][0] == google.received - 0 or seen[-1][0] <= file.stat().st_size
    start = next(h for m, p, h, b in google.requests if p.startswith("/upload"))
    assert start["authorization"] == f"Bearer {SECRET}"
    assert start["x-upload-content-type"] == "video/mp4"
    body = json.loads(next(b for m, p, h, b in google.requests if p.startswith("/upload")))
    assert body["status"]["privacyStatus"] == "private"


def test_upload_resumes_after_a_server_error(google: Google, tmp_path: Path) -> None:
    google.put_script = [0, 503]  # first chunk ok, second fails once
    vid = make_client(google, tmp_path).upload(big_file(tmp_path), yt.video_resource(details()))
    assert vid == "VID_abc123"
    assert any(
        h["content-range"].startswith("bytes */") for m, p, h, _ in google.requests if m == "PUT"
    )


def test_upload_gives_up_after_repeated_failures(google: Google, tmp_path: Path) -> None:
    google.put_script = [503] * 40
    with pytest.raises(YouTubeUploadError, match="kept failing"):
        make_client(google, tmp_path).upload(big_file(tmp_path), yt.video_resource(details()))


def test_quota_error_when_starting_is_classified(google: Google, tmp_path: Path) -> None:
    google.start_status = 403
    google.start_body = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
    with pytest.raises(YouTubeQuotaError, match="quota"):
        make_client(google, tmp_path).upload(big_file(tmp_path), yt.video_resource(details()))
    assert google.count("PUT", "/session") == 0


def test_auth_error_when_starting_is_classified(google: Google, tmp_path: Path) -> None:
    google.start_status = 401
    with pytest.raises(YouTubeAuthError):
        make_client(google, tmp_path).upload(big_file(tmp_path), yt.video_resource(details()))


def test_cancel_stops_between_chunks(google: Google, tmp_path: Path) -> None:
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(JobCancelledError):
        make_client(google, tmp_path).upload(
            big_file(tmp_path), yt.video_resource(details()), cancelled=cancelled
        )
    assert google.count("PUT", "/session") == 1


def test_disconnect_deletes_the_token(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path)
    assert client.connected()
    client.store.delete()
    assert not client.connected()


# ------------------------------------------------------------------ service


@pytest.fixture
def ready(
    settings: Settings,
    db_conn: sqlite3.Connection,
    project: ShortProject,
    video: Video,
) -> PublishingService:
    service = PublishingService(settings, db_conn, None)
    done(service, project, video)
    service.save_rights(
        video.id, rt.Rights(status=rt.ORIGINAL, source_title="Talk", creator="me"), confirmed=True
    )
    export(settings, db_conn, video, project)
    assert service.confirm_ready(project.id).status == rd.READY
    return service


def make_service(
    settings: Settings, conn: sqlite3.Connection, pub: PublishingService, g: Google, tmp_path: Path
) -> YouTubeUploadService:
    client = make_client(g, tmp_path)
    return YouTubeUploadService(settings, conn, pub, lambda: client)


def test_prepare_requires_a_confirmed_checklist(
    settings: Settings,
    db_conn: sqlite3.Connection,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    pub = PublishingService(settings, db_conn, None)
    svc = make_service(settings, db_conn, pub, google, tmp_path)
    with pytest.raises(PublishError, match="not ready"):
        svc.prepare(project.id)
    assert google.count("POST", "/upload") == 0


def test_prepare_shows_exact_details_and_sends_nothing(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    result = svc.prepare(project.id)
    assert result.record and result.details
    d = result.details
    assert d.channel_id == "UC123" and d.visibility == "private" and d.title == "The park meeting"
    assert len(d.file_sha256) == 64 and d.file_name.endswith(".mp4")
    assert result.record.status == yt.DRAFT
    assert google.count("POST", "/upload") == 0 and google.count("PUT", "/session") == 0


def test_public_visibility_cannot_reach_an_upload(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    ready.set_setting(project.id, "visibility", "public")
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    with pytest.raises(PublishError, match="not ready"):  # the change voids the confirmation
        svc.prepare(project.id)
    assert google.count("POST", "/upload") == 0


def test_approval_needs_the_exact_typed_text(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    for wrong in ("", "yes", "I approve", "i approve this upload"):
        with pytest.raises(UploadNotApprovedError):
            svc.approve(record.id, wrong)
    assert svc.repo.get(record.id).status == yt.DRAFT  # type: ignore[union-attr]
    assert (
        db_conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = ?", (UPLOAD_YOUTUBE,)).fetchone()[0]
        == 0
    )
    assert google.count("POST", "/upload") == 0


def test_full_flow_uploads_only_after_approval_and_stays_private(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    assert google.count("POST", "/upload") == 0
    job_id = svc.approve(record.id, yt.APPROVAL_TEXT)
    assert job_id > 0 and svc.repo.get(record.id).status == yt.APPROVED  # type: ignore[union-attr]
    assert google.count("POST", "/upload") == 0  # still nothing sent until the worker runs
    finished = svc.run_upload(record.id)
    assert finished.status == yt.UPLOADED and finished.youtube_video_id == "VID_abc123"
    assert finished.studio_url == "https://studio.youtube.com/video/VID_abc123/edit"
    body = json.loads(next(b for m, p, h, b in google.requests if p.startswith("/upload")))
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["title"] == "The park meeting"


def test_run_upload_refuses_an_unapproved_record(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    with pytest.raises(UploadNotApprovedError):
        svc.run_upload(record.id)
    assert google.count("POST", "/upload") == 0


def test_cannot_run_the_same_approval_twice(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    svc.run_upload(record.id)
    with pytest.raises(UploadNotApprovedError):
        svc.run_upload(record.id)
    assert google.count("POST", "/upload") == 1


def test_editing_after_the_draft_voids_the_approval(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    ready.edit_metadata(project.id, description="Sneaky change")
    with pytest.raises((UploadNotApprovedError, PublishError)):
        svc.approve(record.id, yt.APPROVAL_TEXT)
    assert google.count("POST", "/upload") == 0


def test_change_between_approval_and_upload_blocks_the_upload(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    ready.edit_metadata(project.id, selected_title="A different title")
    with pytest.raises(UploadNotApprovedError):
        svc.run_upload(record.id)
    after = svc.repo.get(record.id)
    assert after and after.status == yt.FAILED and google.count("POST", "/upload") == 0


def test_file_tampered_after_approval_blocks_the_upload(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    render = ready.latest_export(project.id)
    assert render is not None and render.output_path is not None
    render.output_path.write_bytes(b"y" * 100)  # same size, different bytes
    with pytest.raises(UploadNotApprovedError):
        svc.run_upload(record.id)
    assert google.count("POST", "/upload") == 0


def test_a_different_channel_blocks_the_upload(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    google.channel = {"id": "UC999", "snippet": {"title": "Someone else"}}
    with pytest.raises(UploadNotApprovedError, match="channel"):
        svc.run_upload(record.id)
    assert google.count("POST", "/upload") == 0


def test_quota_failure_is_recorded_and_retry_reverifies(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    google.start_status = 403
    google.start_body = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
    with pytest.raises(YouTubeQuotaError):
        svc.run_upload(record.id)
    failed = svc.repo.get(record.id)
    assert failed and failed.status == yt.FAILED and failed.error_kind == yt.KIND_QUOTA
    google.start_status = 200
    svc.retry(record.id)
    assert svc.run_upload(record.id).status == yt.UPLOADED


def test_duplicate_upload_needs_explicit_opt_in(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    first = svc.prepare(project.id).record
    assert first
    svc.approve(first.id, yt.APPROVAL_TEXT)
    svc.run_upload(first.id)
    second = svc.prepare(project.id).record
    assert second
    with pytest.raises(UploadNotApprovedError, match="already uploaded"):
        svc.approve(second.id, yt.APPROVAL_TEXT)
    assert svc.approve(second.id, yt.APPROVAL_TEXT, allow_duplicate=True) > 0


def test_studio_daily_limit_blocks_approval(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    limited = Settings(
        data_dir=settings.data_dir, models_dir=settings.models_dir, youtube_daily_upload_limit=1
    )
    svc = YouTubeUploadService(limited, db_conn, ready, lambda: make_client(google, tmp_path))
    first = svc.prepare(project.id).record
    assert first
    svc.approve(first.id, yt.APPROVAL_TEXT)
    svc.run_upload(first.id)
    second = svc.prepare(project.id).record
    assert second
    with pytest.raises(YouTubeQuotaError, match="daily limit"):
        svc.approve(second.id, yt.APPROVAL_TEXT, allow_duplicate=True)


def test_cancel_before_start_and_draft_replacement(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    svc = make_service(settings, db_conn, ready, google, tmp_path)
    a = svc.prepare(project.id).record
    b = svc.prepare(project.id).record
    assert a and b
    assert svc.repo.get(a.id).status == yt.CANCELLED  # type: ignore[union-attr]
    svc.approve(b.id, yt.APPROVAL_TEXT)
    assert svc.cancel(b.id)
    with pytest.raises(UploadNotApprovedError):
        svc.run_upload(b.id)


def test_state_survives_reopening_and_secrets_never_reach_sqlite(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
    google: Google,
    tmp_path: Path,
) -> None:
    from iftyedits.storage.db import open_database

    svc = make_service(settings, db_conn, ready, google, tmp_path)
    record = svc.prepare(project.id).record
    assert record
    svc.approve(record.id, yt.APPROVAL_TEXT)
    svc.run_upload(record.id)
    db_conn.close()
    fresh = open_database(settings.db_path)
    try:
        from iftyedits.storage.youtube import YouTubeRepository

        got = YouTubeRepository(fresh).get(record.id)
        assert got and got.status == yt.UPLOADED
        dump = "\n".join(fresh.iterdump())
        for secret in (SECRET, REFRESH, "csecret"):
            assert secret not in dump
    finally:
        fresh.close()


def test_connection_status_without_configuration(
    settings: Settings, db_conn: sqlite3.Connection, ready: PublishingService
) -> None:
    svc = YouTubeUploadService(settings, db_conn, ready)
    state = svc.connection()
    assert not state.configured and "not set up" in state.problem.lower()
    with pytest.raises(YouTubeNotConfiguredError):
        svc.prepare(1)


def test_package_export_still_works_without_youtube(
    settings: Settings,
    db_conn: sqlite3.Connection,
    ready: PublishingService,
    project: ShortProject,
) -> None:
    from iftyedits.services.upload_package import build_package

    assert build_package(ready, project.id)["title"] == "The park meeting"
