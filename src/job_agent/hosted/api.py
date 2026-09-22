"""Token-protected hosted control-plane API.

This API is safe to expose behind HTTPS because it does not serve the local
browser-driving dashboard and does not run pipeline phases in the request thread.
It records run requests in a durable queue for isolated workers.
"""

from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from job_agent.hosted.queue import HostedQueue

MAX_BODY_BYTES = 128 * 1024


class HostedApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_class, *, queue: Optional[HostedQueue] = None, identities=None):
        super().__init__(address, handler_class)
        self.queue = queue or HostedQueue()
        from job_agent.hosted.auth import HostedIdentityStore
        self.identities = identities or HostedIdentityStore(self.queue)


class HostedApiHandler(BaseHTTPRequestHandler):
    server_version = "JobAgentHostedAPI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Invalid Content-Length.")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        self.user_id = self.server.identities.authenticate(supplied[len(prefix):]) if supplied.startswith(prefix) else None
        return self.user_id is not None

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/health":
            self._send(HTTPStatus.OK, {"ok": True, "service": "job-agent-hosted-api"})
            return
        if route == "/ready":
            with self.server.identities.connect() as connection:
                connection.execute('SELECT 1')
            self._send(HTTPStatus.OK, {"ok": True})
            return
        if route in ("/jobs", "/jobs/stats"):
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Missing or invalid bearer token."})
                return
            if route == "/jobs/stats":
                self._send(HTTPStatus.OK, {"jobs": self.server.identities.job_count(self.user_id)})
                return
            query = parse_qs(urlparse(self.path).query)

            def one(name, cast, default=None):
                values = query.get(name)
                try:
                    return cast(values[0]) if values else default
                except (TypeError, ValueError):
                    return default

            self._send(HTTPStatus.OK, {"jobs": self.server.identities.jobs(self.user_id, one("limit", int, 50) or 50)})
            return
        if route.startswith("/runs/"):
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Missing or invalid bearer token."})
                return
            try:
                run_id = int(route.rsplit("/", 1)[1])
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Run id must be an integer."})
                return
            run = self.server.queue.get(run_id)
            if run is None or run.user_id != self.user_id:
                self._send(HTTPStatus.NOT_FOUND, {"error": "Run not found."})
                return
            self._send(HTTPStatus.OK, {"run": run.__dict__})
            return
        self._send(HTTPStatus.NOT_FOUND, {"error": f"No route for {route}"})

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/runs":
            self._send(HTTPStatus.NOT_FOUND, {"error": f"No route for {route}"})
            return
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "Missing or invalid bearer token."})
            return
        try:
            payload = self._read_json()
            if payload.get("user_id") not in (None, self.user_id):
                self._send(HTTPStatus.FORBIDDEN, {"error": "user_id must match the authenticated user."})
                return
            run = self.server.queue.enqueue(
                user_id=self.user_id,
                phases=list(payload.get("phases") or []),
                options=dict(payload.get("options") or {}),
            )
        except Exception as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send(HTTPStatus.ACCEPTED, {"run": run.__dict__})


def run(host: str = "0.0.0.0", port: int = 8080) -> None:
    server = HostedApiServer((host, port), HostedApiHandler)
    print(f"Hosted control-plane API listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down hosted control-plane API.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run(
        host=os.environ.get("HOSTED_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("HOSTED_API_PORT", "8080")),
    )
