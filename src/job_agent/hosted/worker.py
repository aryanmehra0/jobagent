"""Hosted queue worker.

The worker claims queued runs and records terminal state. By default it remains
in validation mode. When ``HOSTED_WORKER_EXECUTE=1`` is set, it executes the
queued phases in a subprocess whose data/output/browser directories are scoped
to the hosted user, so the API process never runs browser/LLM work inline and
users do not share a Playwright profile.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from job_agent.config.settings import settings
from job_agent.hosted.auth import HostedIdentityStore
from job_agent.hosted.queue import HostedQueue

RUNNER_CODE = """
import json
from job_agent.web.runner import PipelineRunner
phases = json.loads(__import__("os").environ["HOSTED_RUN_PHASES_JSON"])
options = json.loads(__import__("os").environ["HOSTED_RUN_OPTIONS_JSON"])
PipelineRunner().run_sync(phases, options)
"""


def _workspace_for(user_id: str) -> Path:
    root = Path(os.environ.get("HOSTED_WORKER_WORKSPACE_DIR") or (settings.data_dir / "hosted_workspaces"))
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id).strip("._") or "user"
    workspace = (root / safe_user).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    for child in ("raw_resumes", "profiles", "outputs", "browser_profile", "artifacts"):
        (workspace / child).mkdir(parents=True, exist_ok=True)
    return workspace


def _expanded_phases(phases: list[str], options: dict) -> list[str]:
    if phases == ["run-pipeline"]:
        ordered = ["intake", "source", "evaluate", "tailor", "apply", "track", "prep"]
        if options.get("skip_intake"):
            ordered.remove("intake")
        return ordered
    return phases


def _hosted_worker_isolation_mode() -> str:
    mode = os.environ.get("HOSTED_WORKER_ISOLATION_MODE", settings.hosted_worker_isolation_mode).strip().lower()
    if mode not in {"workspace", "container"}:
        raise RuntimeError("HOSTED_WORKER_ISOLATION_MODE must be 'workspace' or 'container'.")
    return mode


def _child_env(queue: HostedQueue, *, user_id: str, phases: list[str], options: dict, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    paths = {
        "JOB_AGENT_DATA_DIR": workspace,
        "JOB_AGENT_RAW_RESUMES_DIR": workspace / "raw_resumes",
        "JOB_AGENT_PROFILES_DIR": workspace / "profiles",
        "JOB_AGENT_PROFILE_PATH": workspace / "profiles" / "profile.json",
        "JOB_AGENT_OUTPUTS_DIR": workspace / "outputs",
        "JOB_AGENT_BROWSER_PROFILE_DIR": workspace / "browser_profile",
        "JOB_AGENT_ARTIFACT_STORAGE_DIR": workspace / "artifacts",
        "JOB_AGENT_TRACKER_PATH": workspace / "outputs" / "applications_tracker.xlsx",
    }
    env.update({name: str(path) for name, path in paths.items()})
    env["HOSTED_RUN_PHASES_JSON"] = json.dumps(_expanded_phases(phases, options))
    env["HOSTED_RUN_OPTIONS_JSON"] = json.dumps(options)
    env["JOB_AGENT_HOSTED_USER_ID"] = user_id
    env.setdefault("ANONYMIZED_TELEMETRY", "false")
    env.setdefault("PLAYWRIGHT_HEADLESS", "true")
    if not queue.database_url:
        env["DATABASE_URL"] = ""
    existing_pythonpath = env.get("PYTHONPATH")
    src_path = str(settings.base_dir / "src")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else os.pathsep.join([src_path, existing_pythonpath])
    return env


def _execute_run(queue: HostedQueue, run, workspace: Optional[Path] = None) -> Path:
    expanded = _expanded_phases(run.phases, run.options)
    if "apply" in expanded and not run.options.get("dry_run", True):
        if os.environ.get("HOSTED_WORKER_ALLOW_LIVE_APPLY") != "1":
            raise RuntimeError("Hosted worker refuses live apply unless HOSTED_WORKER_ALLOW_LIVE_APPLY=1.")
        isolation_mode = _hosted_worker_isolation_mode()
        if settings.app_environment in {"staging", "production"} and isolation_mode != "container":
            raise RuntimeError(
                "Hosted worker refuses staging/production live apply unless "
                "HOSTED_WORKER_ISOLATION_MODE=container."
            )

    workspace = workspace or _workspace_for(run.user_id)
    timeout = float(os.environ.get("HOSTED_WORKER_RUN_TIMEOUT_SECONDS", "3600"))
    heartbeat = max(1.0, float(os.environ.get("HOSTED_WORKER_HEARTBEAT_SECONDS", "15")))
    log_path = workspace / "outputs" / f"hosted_run_{run.id}.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", RUNNER_CODE],
            cwd=str(settings.base_dir),
            env=_child_env(queue, user_id=run.user_id, phases=expanded, options=run.options, workspace=workspace),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while True:
            code = process.poll()
            if code is not None:
                if code:
                    tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
                    raise RuntimeError(f"Hosted worker subprocess exited {code}: {tail}")
                return workspace
            if time.monotonic() - started > timeout:
                process.kill()
                raise TimeoutError(f"Hosted worker subprocess exceeded {timeout:g} seconds.")
            queue.heartbeat(run.id)
            time.sleep(heartbeat)


@contextmanager
def _worker_database(queue: HostedQueue, user_id: str, workspace: Optional[Path] = None):
    """Open the jobs DB for the hosted worker context, restoring global settings."""
    from job_agent.storage.jobs_db import JobsDatabase

    previous = {
        "profile_path": settings.profile_path,
        "outputs_dir": settings.outputs_dir,
        "hosted_user_id": settings.hosted_user_id,
    }
    try:
        if workspace is not None:
            settings.profile_path = workspace / "profiles" / "profile.json"
            settings.outputs_dir = workspace / "outputs"
        settings.hosted_user_id = user_id
        if queue.database_url:
            yield JobsDatabase(database_url=queue.database_url)
        elif workspace is not None and (workspace / "outputs" / "jobs.db").is_file():
            yield JobsDatabase(db_path=workspace / "outputs" / "jobs.db")
        else:
            yield JobsDatabase()
    finally:
        settings.profile_path = previous["profile_path"]
        settings.outputs_dir = previous["outputs_dir"]
        settings.hosted_user_id = previous["hosted_user_id"]


def _record_worker_event(
    queue: HostedQueue,
    run,
    event_type: str,
    *,
    workspace: Optional[Path] = None,
    success: Optional[bool] = None,
    latency_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort worker observability; never fail the queue state transition."""
    safe_metadata = {
        "hosted_run_id": run.id,
        "attempts": run.attempts,
        "phases": _expanded_phases(run.phases, run.options),
        **(metadata or {}),
    }
    try:
        with _worker_database(queue, run.user_id, workspace) as database:
            database.record_run_event(
                phase="hosted_worker",
                event_type=event_type,
                run_id=f"hosted:{run.id}",
                candidate_id=run.user_id,
                success=success,
                latency_ms=latency_ms,
                error_code=error_code,
                metadata=safe_metadata,
            )
    except Exception:
        pass


def _publish_user_jobs(queue: HostedQueue, user_id: str, workspace: Path, *, limit: int = 500) -> int:
    """Publish a completed worker's DB-backed jobs into the hosted API partition."""
    sqlite_db = workspace / "outputs" / "jobs.db"
    if not queue.database_url and not sqlite_db.is_file():
        return 0

    with _worker_database(queue, user_id, workspace) as database:
        jobs = database.jobs(limit=limit)

    return HostedIdentityStore(queue).put_jobs(user_id, jobs)


def run_once(queue: HostedQueue) -> bool:
    run = queue.claim_next()
    if run is None:
        return False
    started = time.monotonic()
    try:
        workspace = None
        if os.environ.get("HOSTED_WORKER_EXECUTE") == "1":
            workspace = _workspace_for(run.user_id)
            _record_worker_event(queue, run, "execution_started", workspace=workspace)
            workspace = _execute_run(queue, run, workspace)
        if workspace is not None:
            published = _publish_user_jobs(queue, run.user_id, workspace)
            _record_worker_event(
                queue, run, "jobs_published", workspace=workspace, success=True,
                metadata={"published_jobs": published},
            )
        else:
            _record_worker_event(queue, run, "validation_succeeded", success=True)
        _record_worker_event(
            queue, run, "succeeded", workspace=workspace, success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        queue.finish(run.id, ok=True)
    except Exception as exc:
        permanent = (
            "HOSTED_WORKER_ALLOW_LIVE_APPLY" in str(exc)
            or "HOSTED_WORKER_ISOLATION_MODE" in str(exc)
        )
        _record_worker_event(
            queue, run, "failed", success=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=exc.__class__.__name__,
        )
        queue.finish(run.id, ok=False, error=str(exc), retryable=not permanent)
    return True


def run_forever(poll_seconds: float = 2.0) -> None:
    queue = HostedQueue()
    print("Hosted queue worker started.")
    while True:
        worked = run_once(queue)
        if not worked:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_forever(float(os.environ.get("HOSTED_WORKER_POLL_SECONDS", "2")))
