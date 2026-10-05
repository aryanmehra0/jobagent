"""FastAPI hosted control-plane application.

The local dashboard remains a loopback-only standard-library server. This ASGI
app is the production-facing control plane: typed schemas, OpenAPI, request IDs,
stable errors, authenticated user ownership, and background-run enqueueing.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from job_agent.config.settings import settings
from job_agent.hosted.auth import HostedIdentityStore
from job_agent.hosted.queue import HostedQueue, VALID_PHASES

security = HTTPBearer(auto_error=False)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class RunCreateRequest(BaseModel):
    user_id: Optional[str] = None
    phases: list[str] = Field(..., min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None


class RunResponse(BaseModel):
    run: dict[str, Any]


class RunsResponse(BaseModel):
    runs: list[dict[str, Any]]


class RunEventsResponse(BaseModel):
    events: list[dict[str, Any]]


class JobsPageResponse(BaseModel):
    jobs: list[dict[str, Any]]
    next_cursor: Optional[str] = None


class JobStatsResponse(BaseModel):
    jobs: int


class HealthResponse(BaseModel):
    ok: bool
    service: Optional[str] = None


class HostedApiState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    queue: HostedQueue
    identities: HostedIdentityStore
    rate_limit_buckets: dict[tuple[int, str], int] = Field(default_factory=dict)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _json_error(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=ErrorBody(code=code, message=message)).model_dump(),
        headers={"X-Request-ID": request_id},
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or str(uuid.uuid4())


def _rate_limit_identity(request: Request, credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if credentials and credentials.scheme.lower() == "bearer":
        digest = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
        return f"token:{digest}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _check_rate_limit(request: Request, credentials: Optional[HTTPAuthorizationCredentials]) -> None:
    if request.url.path in {"/health", "/ready", "/v1/health", "/v1/ready"}:
        return
    state: HostedApiState = request.app.state.hosted
    window = int(time.time() // 60)
    key = _rate_limit_identity(request, credentials)
    stale = [bucket for bucket in state.rate_limit_buckets if bucket[0] < window]
    for bucket in stale:
        state.rate_limit_buckets.pop(bucket, None)
    bucket = (window, key)
    count = state.rate_limit_buckets.get(bucket, 0)
    if count >= settings.hosted_api_rate_limit_per_minute:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "Too many requests.")
    state.rate_limit_buckets[bucket] = count + 1


def current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    _check_rate_limit(request, credentials)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _error(status.HTTP_401_UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
    state: HostedApiState = request.app.state.hosted
    user_id = state.identities.authenticate(credentials.credentials)
    if user_id is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, "auth_required", "Missing or invalid bearer token.")
    return user_id


def create_app(
    *,
    queue: Optional[HostedQueue] = None,
    identities: Optional[HostedIdentityStore] = None,
) -> FastAPI:
    queue = queue or HostedQueue()
    identities = identities or HostedIdentityStore(queue)
    app = FastAPI(
        title="Job Agent Hosted API",
        version="1.0.0",
        description="Production control plane for user-scoped Job Agent background runs.",
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
        },
    )
    app.state.hosted = HostedApiState(queue=queue, identities=identities)

    allowed_origins = sorted(settings.hosted_allowed_origins)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            max_age=600,
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        origin = request.headers.get("Origin")
        if allowed_origins and origin and origin.rstrip("/") not in allowed_origins:
            return _json_error(status.HTTP_403_FORBIDDEN, "origin_forbidden", "Origin is not allowed.", request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
        return _json_error(exc.status_code, detail.get("code", "http_error"), detail.get("message", "Request failed."), _request_id(request))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _json_error(422, "validation_error", str(exc), _request_id(request))

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    @app.get("/v1/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse(ok=True, service="job-agent-hosted-api")

    @app.get("/ready", response_model=HealthResponse, tags=["health"])
    @app.get("/v1/ready", response_model=HealthResponse, tags=["health"])
    def ready(request: Request) -> HealthResponse:
        state: HostedApiState = request.app.state.hosted
        with state.identities.connect() as connection:
            connection.execute("SELECT 1")
        return HealthResponse(ok=True)

    @app.get("/v1/jobs", response_model=JobsPageResponse, tags=["jobs"])
    def jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        after: Optional[str] = None,
        user_id: str = Depends(current_user),
    ) -> JobsPageResponse:
        state: HostedApiState = request.app.state.hosted
        return JobsPageResponse(**state.identities.jobs_page(user_id, limit=limit, after=after))

    @app.get("/v1/jobs/stats", response_model=JobStatsResponse, tags=["jobs"])
    def job_stats(request: Request, user_id: str = Depends(current_user)) -> JobStatsResponse:
        state: HostedApiState = request.app.state.hosted
        return JobStatsResponse(jobs=state.identities.job_count(user_id))

    @app.post("/v1/runs", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED, tags=["runs"])
    def create_run(
        payload: RunCreateRequest,
        request: Request,
        user_id: str = Depends(current_user),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> RunResponse:
        if payload.user_id not in (None, user_id):
            raise _error(status.HTTP_403_FORBIDDEN, "forbidden", "user_id must match the authenticated user.")
        unknown = [phase for phase in payload.phases if phase not in VALID_PHASES]
        if unknown:
            raise _error(status.HTTP_400_BAD_REQUEST, "validation_error", f"Unknown phase(s): {', '.join(unknown)}")
        try:
            run = request.app.state.hosted.queue.enqueue(
                user_id=user_id,
                phases=payload.phases,
                options=payload.options,
                idempotency_key=idempotency_key or payload.idempotency_key,
            )
        except ValueError as exc:
            raise _error(status.HTTP_400_BAD_REQUEST, "validation_error", str(exc)) from None
        return RunResponse(run=asdict(run))

    @app.get("/v1/runs", response_model=RunsResponse, tags=["runs"])
    def list_runs(
        request: Request,
        status_filter: Optional[str] = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=500),
        user_id: str = Depends(current_user),
    ) -> RunsResponse:
        runs = request.app.state.hosted.queue.list_runs(status=status_filter, user_id=user_id, limit=limit)
        return RunsResponse(runs=[asdict(run) for run in runs])

    @app.get("/v1/runs/{run_id}", response_model=RunResponse, tags=["runs"])
    def get_run(run_id: int, request: Request, user_id: str = Depends(current_user)) -> RunResponse:
        run = request.app.state.hosted.queue.get(run_id)
        if run is None or run.user_id != user_id:
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "Run not found.")
        return RunResponse(run=asdict(run))

    @app.get("/v1/runs/{run_id}/events", response_model=RunEventsResponse, tags=["runs"])
    def get_run_events(
        run_id: int,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        user_id: str = Depends(current_user),
    ) -> RunEventsResponse:
        state: HostedApiState = request.app.state.hosted
        run = state.queue.get(run_id)
        if run is None or run.user_id != user_id:
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "Run not found.")
        from job_agent.storage.jobs_db import JobsDatabase

        database = JobsDatabase(database_url=state.queue.database_url) if state.queue.database_url else JobsDatabase()
        events = [
            event for event in database.run_events(run_id=f"hosted:{run_id}", phase="hosted_worker", limit=limit)
            if event.get("candidate_id") == user_id
        ]
        return RunEventsResponse(events=events)

    return app


app = create_app()
