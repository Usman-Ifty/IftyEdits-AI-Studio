"""Local stand-in for Google's OAuth and YouTube upload endpoints. For development and QA only.

It lets the studio's whole YouTube flow run in a browser without a Google account:

    uv run python scripts/dev_youtube_stub.py --port 8700 --delay 0.2

then point the studio at it (see docs/YOUTUBE.md, "Trying it without Google"). It accepts any
sign-in, returns a fixed channel and stores nothing. Real Google behaviour is NOT reproduced.
"""

from __future__ import annotations

import argparse
import http.server
import json
import time
import urllib.parse
from typing import Any

STATE: dict[str, Any] = {"received": 0, "total": 0, "channel": "UC_STUB", "fail": ""}


class Handler(http.server.BaseHTTPRequestHandler):
    delay = 0.2

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/auth":  # auto-approve and bounce back to the studio's loopback
            target = f"{q['redirect_uri'][0]}/?code=STUBCODE&state={q['state'][0]}"
            return self._send(302, headers={"Location": target})
        if parsed.path == "/youtube/v3/channels":
            items = [{"id": STATE["channel"], "snippet": {"title": "Stub Channel"}}]
            return self._send(200, json.dumps({"items": items}).encode())
        if parsed.path == "/admin":  # /admin?channel=UC_OTHER or ?fail=quota
            if "channel" in q:
                STATE["channel"] = q["channel"][0]
            if "fail" in q:
                STATE["fail"] = q["fail"][0]
            return self._send(200, json.dumps(STATE).encode())
        self._send(404)

    def do_POST(self) -> None:
        body = self._body()
        if self.path.startswith("/token"):
            scope = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
            out = {"access_token": "stub-access", "expires_in": 3600, "scope": scope}
            if b"authorization_code" in body:
                out["refresh_token"] = "stub-refresh"  # noqa: S105
            return self._send(200, json.dumps(out).encode())
        if self.path.startswith("/revoke"):
            return self._send(200)
        if self.path.startswith("/upload/youtube/v3/videos"):
            if STATE["fail"] == "quota":
                err = {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}
                return self._send(403, json.dumps(err).encode())
            STATE["total"] = int(self.headers["X-Upload-Content-Length"])
            STATE["received"] = 0
            host = self.headers["Host"]
            return self._send(200, b"{}", {"Location": f"http://{host}/session/1"})
        self._send(404)

    def do_PUT(self) -> None:
        body = self._body()
        time.sleep(self.delay)
        STATE["received"] += len(body)
        if STATE["received"] >= STATE["total"]:
            return self._send(200, json.dumps({"id": "STUB_VIDEO_1"}).encode())
        self._send(308, headers={"Range": f"bytes=0-{STATE['received'] - 1}"})

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--delay", type=float, default=0.2, help="seconds per uploaded chunk")
    args = parser.parse_args()
    Handler.delay = args.delay
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"YouTube stub on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
