"""Token-protected hosted control-plane API.

This API is safe to expose behind HTTPS because it does not serve the local
browser-driving dashboard and does not run pipeline phases in the request thread.
It records run requests in a durable queue for isolated workers.
"""

from __future__ import annotations

import json
import os
import hashlib
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from job_agent.config.settings import settings
from job_agent.hosted.queue import HostedQueue

MAX_BODY_BYTES = 128 * 1024


class HostedApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_class, *, queue: Optional[HostedQueue] = None, identities=None):
        super().__init__(address, handler_class)
        self.queue = queue or HostedQueue()
        self.rate_limit_buckets: dict[tuple[int, str], int] = {}
        self.rate_limit_lock = threading.Lock()
        from job_agent.hosted.auth import HostedIdentityStore
        self.identities = identities or HostedIdentityStore(self.queue)


class HostedApiHandler(BaseHTTPRequestHandler):
    server_version = "JobAgentHostedAPI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, status: HTTPStatus, payload: Any) -> None:
        request_id = getattr(self, "request_id", None) or self.headers.get("X-Request-ID") or str(uuid.uuid4())
        self.request_id = request_id
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", request_id)
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _send_cors_headers(self) -> None:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return
        normalized = origin.rstrip("/")
        if normalized in settings.hosted_allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _cors_allowed(self) -> bool:
        allowed_origins = settings.hosted_allowed_origins
        origin = (self.headers.get("Origin") or "").strip()
        if not allowed_origins or not origin:
            return True
        if origin.rstrip("/") in allowed_origins:
            return True
        self._error(HTTPStatus.FORBIDDEN, "origin_forbidden", "Origin is not allowed.")
        return False

    def _rate_limit_allowed(self, route: str) -> bool:
        if route in {"/health", "/ready"}:
            return True
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if supplied.startswith(prefix):
            token_hash = hashlib.sha256(supplied[len(prefix):].encode("utf-8")).hexdigest()
            key = f"token:{token_hash}"
        else:
            key = f"ip:{self.client_address[0]}"
        window = int(time.time() // 60)
        with self.server.rate_limit_lock:
            stale = [bucket for bucket in self.server.rate_limit_buckets if bucket[0] < window]
            for bucket in stale:
                self.server.rate_limit_buckets.pop(bucket, None)
            bucket = (window, key)
            count = self.server.rate_limit_buckets.get(bucket, 0)
            if count >= settings.hosted_api_rate_limit_per_minute:
                self._error(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited", "Too many requests.")
                return False
            self.server.rate_limit_buckets[bucket] = count + 1
        return True

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        if not self._cors_allowed():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", self.request_id)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-Request-ID")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

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
        self.request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        route = urlparse(self.path).path
        if not self._cors_allowed() or not self._rate_limit_allowed(route):
            return
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
                self._error(HTTPStatus.UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
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

            self._send(HTTPStatus.OK, self.server.identities.jobs_page(
                self.user_id,
                limit=one("limit", int, 50) or 50,
                after=one("after", str, None),
            ))
            return
        if route == "/runs":
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(500, int((query.get("limit") or ["50"])[0])))
            except (TypeError, ValueError):
                limit = 50
            status_filter = (query.get("status") or [None])[0]
            runs = self.server.queue.list_runs(status=status_filter, user_id=self.user_id, limit=limit)
            self._send(HTTPStatus.OK, {"runs": [run.__dict__ for run in runs]})
            return
        if route.startswith("/runs/"):
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
                return
            wants_events = route.endswith("/events")
            run_part = route.removesuffix("/events").rsplit("/", 1)[1] if wants_events else route.rsplit("/", 1)[1]
            try:
                run_id = int(run_part)
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "validation_error", "Run id must be an integer.")
                return
            run = self.server.queue.get(run_id)
            if run is None or run.user_id != self.user_id:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Run not found.")
                return
            if wants_events:
                from job_agent.storage.jobs_db import JobsDatabase

                query = parse_qs(urlparse(self.path).query)
                try:
                    limit = max(1, min(500, int((query.get("limit") or ["100"])[0])))
                except (TypeError, ValueError):
                    limit = 100
                database = (
                    JobsDatabase(database_url=self.server.queue.database_url)
                    if self.server.queue.database_url else JobsDatabase()
                )
                events = [
                    event for event in database.run_events(
                        run_id=f"hosted:{run_id}", phase="hosted_worker", limit=limit
                    )
                    if event.get("candidate_id") == self.user_id
                ]
                self._send(HTTPStatus.OK, {"events": events})
                return
            self._send(HTTPStatus.OK, {"run": run.__dict__})
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", f"No route for {route}")

    def do_POST(self) -> None:  # noqa: N802
        self.request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        route = urlparse(self.path).path
        if not self._cors_allowed() or not self._rate_limit_allowed(route):
            return
        if route != "/runs":
            self._error(HTTPStatus.NOT_FOUND, "not_found", f"No route for {route}")
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
            return
        try:
            payload = self._read_json()
            if payload.get("user_id") not in (None, self.user_id):
                self._error(HTTPStatus.FORBIDDEN, "forbidden", "user_id must match the authenticated user.")
                return
            run = self.server.queue.enqueue(
                user_id=self.user_id,
                phases=list(payload.get("phases") or []),
                options=dict(payload.get("options") or {}),
                idempotency_key=self.headers.get("Idempotency-Key") or payload.get("idempotency_key"),
            )
        except Exception as exc:
            self._error(HTTPStatus.BAD_REQUEST, "validation_error", str(exc))
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
