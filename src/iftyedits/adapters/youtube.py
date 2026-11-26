"""YouTube Data API client: OAuth (loopback + PKCE), token file and resumable upload.

Standard library only (decision D33 applies: one blocking request at a time).

Security:

* The OAuth client file and the token live outside the repository. The token file is written
  with mode 0600 in a 0700 directory, atomically, and refused if it would sit inside the project.
* Tokens, the client secret and authorisation codes are never logged and never put in an error
  message or in SQLite.
* Sign-in uses the system browser and a loopback address (127.0.0.1) with PKCE and a random
  ``state`` check.
* Nothing in this module decides whether an upload may happen. The service does that.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iftyedits.core import youtube as yt
from iftyedits.core.errors import (
    JobCancelledError,
    YouTubeAuthError,
    YouTubeError,
    YouTubeNotConfiguredError,
    YouTubeQuotaError,
    YouTubeUploadError,
)

log = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
_CHUNK_MULTIPLE = 256 * 1024
_MAX_RETRIES = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def default_token_path() -> Path:
    """``~/.config/iftyedits/youtube_token.json``."""
    return Path.home() / ".config" / "iftyedits" / "youtube_token.json"


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """The OAuth client from Google Cloud (Desktop app)."""

    client_id: str
    client_secret: str
    auth_uri: str
    token_uri: str


def load_client_config(path: Path | None) -> ClientConfig:
    """Read the downloaded client JSON. Raises if missing or the wrong type."""
    if path is None:
        raise YouTubeNotConfiguredError(
            "YouTube is not set up. Set IFTYEDITS_YOUTUBE_CLIENT_SECRETS to your OAuth client "
            "file (see docs/YOUTUBE.md). Manual export still works without it."
        )
    file = path.expanduser()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise YouTubeNotConfiguredError(
            f"The OAuth client file was not found: {file.name}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise YouTubeNotConfiguredError("The OAuth client file could not be read as JSON.") from exc
    block = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(block, dict) or not block.get("client_id") or not block.get("client_secret"):
        raise YouTubeNotConfiguredError(
            "That file is not a Desktop-app OAuth client. In Google Cloud, create an OAuth client "
            "of type “Desktop app” and download its JSON."
        )
    auth_uri = str(block.get("auth_uri") or AUTH_URI)
    token_uri = str(block.get("token_uri") or TOKEN_URI)
    if not (yt.trusted_endpoint(auth_uri) and yt.trusted_endpoint(token_uri)):
        raise YouTubeNotConfiguredError(
            "The OAuth client file points to an address that is not Google's, so it was not used "
            "(your credentials would be sent there)."
        )
    return ClientConfig(str(block["client_id"]), str(block["client_secret"]), auth_uri, token_uri)


class TokenStore:
    """The sign-in token on disk: 0600, outside the repository, written atomically."""

    def __init__(self, path: Path, forbidden_root: Path | None = None) -> None:
        self.path = path.expanduser().resolve()
        if forbidden_root is not None:
            root = forbidden_root.resolve()
            if self.path == root or root in self.path.parents:
                raise YouTubeNotConfiguredError(
                    "The token file must be outside the project folder so it cannot be "
                    "committed. Set IFTYEDITS_YOUTUBE_TOKEN_PATH to a folder elsewhere."
                )

    def exists(self) -> bool:
        """Whether a token file is present."""
        return self.path.is_file()

    def load(self) -> dict[str, Any] | None:
        """The saved token, or None. Loose permissions are tightened."""
        if not self.path.is_file():
            return None
        try:
            if self.path.stat().st_mode & 0o077:
                self.path.chmod(0o600)
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, data: dict[str, Any]) -> None:
        """Write atomically with mode 0600."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            log.debug("could not tighten token directory permissions")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".yt-token-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self) -> None:
        """Remove the token file (disconnect)."""
        self.path.unlink(missing_ok=True)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict[str, str]
    done: threading.Event

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        type(self).result.update({k: v[0] for k, v in query.items()})
        body = (
            b"<html><body style='font-family:sans-serif'><h3>IftyEdits is connected.</h3>"
            b"<p>You can close this tab and return to the studio.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).done.set()

    def log_message(self, *args: object) -> None:
        pass


class YouTubeClient:
    """OAuth plus the few YouTube Data API calls the studio needs."""

    def __init__(
        self,
        client: ClientConfig,
        store: TokenStore,
        *,
        timeout_s: float = 60.0,
        chunk_bytes: int = 8 * 1024 * 1024,
        api_base: str = API_BASE,
        upload_base: str = UPLOAD_BASE,
        sleep: Callable[[float], None] = time.sleep,
        open_browser: Callable[[str], object] = webbrowser.open,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.store = store
        for base in (api_base, upload_base):
            if not yt.trusted_endpoint(base):
                raise YouTubeNotConfiguredError(
                    "The YouTube API address must be on googleapis.com (or this computer)."
                )
        self._timeout = timeout_s
        self._chunk = max(_CHUNK_MULTIPLE, chunk_bytes - chunk_bytes % _CHUNK_MULTIPLE)
        self._api = api_base.rstrip("/")
        self._upload = upload_base.rstrip("/")
        self._sleep = sleep
        self._open = open_browser
        self._clock = clock

    # ------------------------------------------------------------------ sign-in

    def connected(self) -> bool:
        """Whether a usable refresh token is saved (not whether Google still accepts it)."""
        return self.store.load() is not None

    def authorize(self, wait_s: float = 180.0, on_url: Callable[[str], None] | None = None) -> None:
        """Browser sign-in on a loopback address. Blocks until done, cancelled or timed out."""
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        handler = type("Handler", (_Callback,), {"result": {}, "done": threading.Event()})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        redirect = f"http://127.0.0.1:{port}"
        url = (
            self.client.auth_uri
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": self.client.client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": " ".join(yt.SCOPES),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if on_url:
                on_url(url)
            self._open(url)
            if not handler.done.wait(wait_s):  # type: ignore[attr-defined]
                raise YouTubeAuthError("Sign-in was not completed in time.", yt.KIND_AUTH)
        finally:
            server.shutdown()
            server.server_close()
        result: dict[str, str] = handler.result  # type: ignore[attr-defined]
        if "error" in result:
            raise YouTubeAuthError("Google sign-in was cancelled or refused.", yt.KIND_AUTH)
        if result.get("state") != state:
            raise YouTubeAuthError("Sign-in response did not match this request.", yt.KIND_AUTH)
        token = self._token_request(
            {
                "code": result["code"],
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            }
        )
        granted = set(str(token.get("scope", "")).split())
        if yt.SCOPES[0] not in granted:
            raise YouTubeAuthError(
                "Upload permission was not granted. Connect again and allow uploading.",
                yt.KIND_AUTH,
            )
        if not token.get("refresh_token"):
            raise YouTubeAuthError(
                "Google did not return a refresh token. Remove the app at "
                "myaccount.google.com/permissions and connect again.",
                yt.KIND_AUTH,
            )
        self.store.save(self._token_record(token, token["refresh_token"]))

    def disconnect(self) -> None:
        """Delete the local token and ask Google to revoke it (best effort)."""
        saved = self.store.load()
        self.store.delete()
        if saved:
            try:
                data = urllib.parse.urlencode({"token": saved["refresh_token"]}).encode()
                revoke = self.client.token_uri.rsplit("/", 1)[0] + "/revoke"
                req = urllib.request.Request(  # noqa: S310
                    revoke,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                _OPENER.open(req, timeout=10).close()
            except (urllib.error.URLError, OSError):
                log.info("token revoke request failed; local token was deleted")

    def _token_record(self, token: dict[str, Any], refresh: str) -> dict[str, Any]:
        return {
            "access_token": token.get("access_token", ""),
            "refresh_token": refresh,
            "expires_at": self._clock() + float(token.get("expires_in", 3600)),
            "scope": token.get("scope", ""),
        }

    def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
                **fields,
            }
        ).encode()
        req = urllib.request.Request(  # noqa: S310
            self.client.token_uri,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _OPENER.open(req, timeout=self._timeout) as response:
                data = json.loads(response.read(100_000))
        except urllib.error.HTTPError as exc:
            raise YouTubeAuthError(
                "Google refused the sign-in (the saved connection may have expired or been "
                "revoked). Connect your YouTube account again.",
                yt.KIND_AUTH,
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise YouTubeError(
                "Could not reach Google to sign in. Check the network.", yt.KIND_NETWORK
            ) from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise YouTubeAuthError("Google sent an unexpected sign-in reply.", yt.KIND_AUTH)
        return data

    def access_token(self) -> str:
        """A valid access token, refreshing it when it is about to expire."""
        saved = self.store.load()
        if saved is None:
            raise YouTubeNotConfiguredError(
                "YouTube is not connected. Connect your account in Settings.", yt.KIND_AUTH
            )
        if float(saved.get("expires_at", 0)) - self._clock() > 60 and saved.get("access_token"):
            return str(saved["access_token"])
        fresh = self._token_request(
            {"refresh_token": str(saved["refresh_token"]), "grant_type": "refresh_token"}
        )
        self.store.save(
            self._token_record(
                {**fresh, "scope": fresh.get("scope", saved.get("scope", ""))},
                str(fresh.get("refresh_token") or saved["refresh_token"]),
            )
        )
        return str(fresh["access_token"])

    # ------------------------------------------------------------------ API

    def _error(self, exc: urllib.error.HTTPError) -> YouTubeError:
        kind, message = yt.classify_api_error(exc.code, exc.read(50_000))
        if kind == yt.KIND_QUOTA:
            return YouTubeQuotaError(message, kind)
        if kind == yt.KIND_AUTH:
            return YouTubeAuthError(message, kind)
        return YouTubeUploadError(message, kind)

    def channel(self) -> tuple[str, str]:
        """``(channel id, title)`` of the signed-in account's channel."""
        req = urllib.request.Request(  # noqa: S310
            f"{self._api}/channels?part=snippet&mine=true",
            headers={"Authorization": f"Bearer {self.access_token()}"},
        )
        try:
            with _OPENER.open(req, timeout=self._timeout) as response:
                data = json.loads(response.read(500_000))
        except urllib.error.HTTPError as exc:
            raise self._error(exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise YouTubeError("Could not reach YouTube.", yt.KIND_NETWORK) from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            raise YouTubeUploadError(
                "This Google account has no YouTube channel to upload to.", yt.KIND_NO_CHANNEL
            )
        first = items[0]
        return str(first["id"]), str(first.get("snippet", {}).get("title", ""))

    def upload(
        self,
        path: Path,
        resource: dict[str, object],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> str:
        """Resumable upload. Returns the new YouTube video id."""
        total = path.stat().st_size
        if total <= 0:
            raise YouTubeUploadError("The export file is empty.", yt.KIND_REJECTED)
        session = self._start_session(resource, total)
        sent = 0
        failures = 0
        with path.open("rb") as handle:
            while True:
                if cancelled():
                    raise JobCancelledError("Upload cancelled before it finished.")
                handle.seek(sent)
                chunk = handle.read(self._chunk)
                if not chunk:
                    raise YouTubeUploadError(
                        "YouTube may have finished the upload but did not confirm it. Check "
                        "YouTube Studio before retrying so you do not upload a duplicate.",
                        yt.KIND_OTHER,
                    )
                end = sent + len(chunk) - 1
                try:
                    status, headers, body = self._put(session, chunk, f"bytes {sent}-{end}/{total}")
                except (urllib.error.URLError, OSError, TimeoutError):
                    status, headers, body = 0, {}, b""
                if status in (200, 201):
                    return self._video_id(body)
                if status == 308:
                    confirmed = self._confirmed(headers)
                    if confirmed > sent:
                        failures = 0
                        sent = confirmed
                    else:
                        failures += 1
                elif status == 0 or status >= 500:
                    failures += 1
                else:
                    kind, message = yt.classify_api_error(status, body)
                    if kind == yt.KIND_QUOTA:
                        raise YouTubeQuotaError(message, kind)
                    if kind == yt.KIND_AUTH:
                        raise YouTubeAuthError(message, kind)
                    raise YouTubeUploadError(message, kind)
                if failures:
                    if failures > _MAX_RETRIES:
                        raise YouTubeUploadError(
                            "The connection to YouTube kept failing. Retry later; the upload "
                            "restarts from the beginning.",
                            yt.KIND_NETWORK,
                        )
                    self._sleep(min(2.0**failures, 30.0))
                    sent = self._resume_offset(session, total)
                if on_progress:
                    on_progress(sent, total)

    def _start_session(self, resource: dict[str, object], total: int) -> str:
        req = urllib.request.Request(  # noqa: S310
            f"{self._upload}/videos?uploadType=resumable&part=snippet,status",
            data=json.dumps(resource).encode(),
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(total),
                "X-Upload-Content-Type": "video/mp4",
            },
            method="POST",
        )
        try:
            with _OPENER.open(req, timeout=self._timeout) as response:
                location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            raise self._error(exc) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise YouTubeError(
                "Could not reach YouTube to start the upload.", yt.KIND_NETWORK
            ) from exc
        if not location:
            raise YouTubeUploadError("YouTube did not return an upload session.", yt.KIND_OTHER)
        return str(location)

    def _put(
        self, session: str, data: bytes, content_range: str
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(  # noqa: S310
            session,
            data=data,
            headers={
                "Content-Length": str(len(data)),
                "Content-Range": content_range,
                "Content-Type": "video/mp4",
            },
            method="PUT",
        )
        try:
            with _OPENER.open(req, timeout=max(self._timeout, 120.0)) as response:
                return response.status, dict(response.headers), response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read(50_000)

    @staticmethod
    def _confirmed(headers: dict[str, str]) -> int:
        rng = {k.lower(): v for k, v in headers.items()}.get("range", "")
        if rng.startswith("bytes=0-"):
            try:
                return int(rng.rsplit("-", 1)[1]) + 1
            except ValueError:
                return 0
        return 0

    def _resume_offset(self, session: str, total: int) -> int:
        try:
            status, headers, _ = self._put(session, b"", f"bytes */{total}")
        except (urllib.error.URLError, OSError, TimeoutError):
            return 0
        if status == 308:
            return self._confirmed(headers)
        if status in (200, 201):
            return total
        return 0

    @staticmethod
    def _video_id(body: bytes) -> str:
        try:
            video_id = str(json.loads(body)["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise YouTubeUploadError(
                "YouTube accepted the file but did not return a video id.", yt.KIND_OTHER
            ) from exc
        return video_id
