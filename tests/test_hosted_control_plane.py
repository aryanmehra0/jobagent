"""Hosted API and queue smoke tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial

from click.testing import CliRunner

from job_agent.config.schema import JobPosting
from job_agent.cli import cli
from job_agent.config.settings import settings
import job_agent.hosted.worker as hosted_worker
from job_agent.hosted.api import HostedApiHandler, HostedApiServer
from job_agent.hosted.auth import HostedIdentityStore
from job_agent.hosted.queue import HostedQueue
from job_agent.hosted.worker import run_once
from job_agent.storage.jobs_db import JobsDatabase


def _request(
    url: str,
    method: str = "GET",
    token: str = "",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    include_headers: bool = False,
):
    request = urllib.request.Request(url, method=method, data=body)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read().decode("utf-8")
            parsed = json.loads(payload) if payload else {}
            if include_headers:
                return response.status, parsed, dict(response.headers)
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        parsed = json.loads(payload) if payload else {}
        if include_headers:
            return exc.code, parsed, dict(exc.headers)
        return exc.code, parsed


def test_hosted_queue_enqueue_claim_and_finish(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    assert queue.backend == "sqlite"
    run = queue.enqueue("user-1", ["source", "evaluate"], {"dry_run": True})

    assert run.status == "pending"
    assert queue.counts() == {"pending": 1}

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.id == run.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.claimed_at is not None

    queue.finish(run.id, ok=True)
    finished = queue.get(run.id)
    assert finished.status == "succeeded"
    assert finished.finished_at is not None


def test_hosted_queue_idempotency_key_returns_existing_user_run(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")

    first = queue.enqueue("user-1", ["source"], {"dry_run": True}, idempotency_key="retry-1")
    duplicate = queue.enqueue("user-1", ["evaluate"], {"dry_run": True}, idempotency_key="retry-1")
    other_user = queue.enqueue("user-2", ["source"], {"dry_run": True}, idempotency_key="retry-1")

    assert duplicate.id == first.id
    assert duplicate.phases == ["source"]
    assert other_user.id != first.id
    assert queue.counts() == {"pending": 2}
    assert [run.id for run in queue.list_runs(user_id="user-1")] == [first.id]
    assert [run.id for run in queue.list_runs(user_id="user-2")] == [other_user.id]


def test_hosted_queue_recovers_stale_running_jobs(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["source"], {"dry_run": True})
    claimed = queue.claim_next()
    assert claimed is not None

    with queue._connect() as conn:
        conn.execute(
            "UPDATE hosted_runs SET heartbeat_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (run.id,),
        )

    assert queue.recover_stale(older_than_seconds=1) == 1
    recovered = queue.get(run.id)
    assert recovered.status == "retryable"

    reclaimed = queue.claim_next()
    assert reclaimed is not None
    assert reclaimed.id == run.id
    assert reclaimed.attempts == 2


def test_hosted_queue_refuses_live_apply_without_explicit_flag(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    try:
        queue.enqueue("user-1", ["apply"], {"dry_run": False})
    except ValueError as exc:
        assert "live apply" in str(exc)
    else:
        raise AssertionError("live apply was accepted without allow_live_apply")


def test_hosted_worker_completes_a_validation_run(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["track"], {"dry_run": True})

    assert run_once(queue) is True
    assert queue.get(run.id).status == "succeeded"
    events = JobsDatabase().run_events(run_id=f"hosted:{run.id}", phase="hosted_worker")
    chronological = list(reversed(events))
    assert [event["event_type"] for event in chronological] == ["validation_succeeded", "succeeded"]
    assert all(event["candidate_id"] == "user-1" for event in events)
    assert run_once(queue) is False


def test_hosted_worker_executes_in_isolated_user_workspace(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["track"], {"dry_run": True})
    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls == 1 else 0

    monkeypatch.setenv("HOSTED_WORKER_EXECUTE", "1")
    monkeypatch.setenv("HOSTED_WORKER_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(hosted_worker.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(hosted_worker.time, "sleep", lambda _seconds: None)

    assert run_once(queue) is True
    finished = queue.get(run.id)
    assert finished.status == "succeeded"
    assert finished.heartbeat_at is not None
    env = captured["kwargs"]["env"]
    assert env["JOB_AGENT_HOSTED_USER_ID"] == "user-1"
    assert env["JOB_AGENT_OUTPUTS_DIR"].endswith(r"workspaces\user-1\outputs") or env["JOB_AGENT_OUTPUTS_DIR"].endswith("workspaces/user-1/outputs")
    assert env["JOB_AGENT_BROWSER_PROFILE_DIR"].endswith(r"workspaces\user-1\browser_profile") or env["JOB_AGENT_BROWSER_PROFILE_DIR"].endswith("workspaces/user-1/browser_profile")
    assert env["DATABASE_URL"] == ""
    database = JobsDatabase(db_path=tmp_path / "workspaces" / "user-1" / "outputs" / "jobs.db")
    events = database.run_events(run_id=f"hosted:{run.id}", phase="hosted_worker")
    chronological = list(reversed(events))
    assert [event["event_type"] for event in chronological] == ["execution_started", "jobs_published", "succeeded"]
    assert chronological[1]["metadata"]["published_jobs"] == 0


def test_hosted_worker_publishes_workspace_jobs_to_user_partition(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    identities = HostedIdentityStore(queue)
    workspace = tmp_path / "workspaces" / "user-1"
    outputs = workspace / "outputs"
    profiles = workspace / "profiles"
    outputs.mkdir(parents=True)
    profiles.mkdir(parents=True)
    profile_path = profiles / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"full_name": "Hosted User", "email": "shared@example.org"},
        "profile_hash": "profile-hosted",
    }), encoding="utf-8")
    job = JobPosting(
        id="j1",
        title="Platform Engineer",
        company="Example",
        location="Remote",
        job_url="https://example.com/jobs/j1",
        description="Build production platforms.",
        source="greenhouse",
        apply_url="https://example.com/apply/j1",
    )
    (outputs / "scraped_jobs.json").write_text(json.dumps([job.model_dump()]), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "hosted_user_id", "user-1")
    JobsDatabase(db_path=outputs / "jobs.db").sync(outputs)

    published = hosted_worker._publish_user_jobs(queue, "user-1", workspace)

    assert published == 1
    assert identities.jobs_page("user-1")["jobs"][0]["job_id"] == "j1"
    assert identities.jobs_page("user-1")["jobs"][0]["title"] == "Platform Engineer"
    assert identities.jobs_page("user-2")["jobs"] == []


def test_hosted_worker_refuses_live_apply_without_worker_flag(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["apply"], {"dry_run": False, "allow_live_apply": True})

    def _unexpected_popen(*_args, **_kwargs):
        raise AssertionError("worker should refuse before starting a subprocess")

    monkeypatch.setenv("HOSTED_WORKER_EXECUTE", "1")
    monkeypatch.delenv("HOSTED_WORKER_ALLOW_LIVE_APPLY", raising=False)
    monkeypatch.setattr(hosted_worker.subprocess, "Popen", _unexpected_popen)

    assert run_once(queue) is True
    failed = queue.get(run.id)
    assert failed.status == "failed"
    assert "HOSTED_WORKER_ALLOW_LIVE_APPLY" in failed.error


def test_hosted_worker_refuses_production_live_apply_without_container_isolation(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["apply"], {"dry_run": False, "allow_live_apply": True})

    def _unexpected_popen(*_args, **_kwargs):
        raise AssertionError("worker should refuse before starting a subprocess")

    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setenv("HOSTED_WORKER_EXECUTE", "1")
    monkeypatch.setenv("HOSTED_WORKER_ALLOW_LIVE_APPLY", "1")
    monkeypatch.setenv("HOSTED_WORKER_ISOLATION_MODE", "workspace")
    monkeypatch.setattr(hosted_worker.subprocess, "Popen", _unexpected_popen)

    assert run_once(queue) is True
    failed = queue.get(run.id)
    assert failed.status == "failed"
    assert "HOSTED_WORKER_ISOLATION_MODE" in failed.error


def test_hosted_worker_rejects_unknown_isolation_mode(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["apply"], {"dry_run": False, "allow_live_apply": True})

    def _unexpected_popen(*_args, **_kwargs):
        raise AssertionError("worker should refuse before starting a subprocess")

    monkeypatch.setenv("HOSTED_WORKER_EXECUTE", "1")
    monkeypatch.setenv("HOSTED_WORKER_ALLOW_LIVE_APPLY", "1")
    monkeypatch.setenv("HOSTED_WORKER_ISOLATION_MODE", "shared")
    monkeypatch.setattr(hosted_worker.subprocess, "Popen", _unexpected_popen)

    assert run_once(queue) is True
    failed = queue.get(run.id)
    assert failed.status == "failed"
    assert "workspace" in failed.error
    assert "container" in failed.error


def test_hosted_worker_allows_production_live_apply_with_container_isolation(tmp_path, monkeypatch):
    queue = HostedQueue(tmp_path / "hosted.db")
    run = queue.enqueue("user-1", ["apply"], {"dry_run": False, "allow_live_apply": True})

    class FakePopen:
        def __init__(self, args, **kwargs):
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls == 1 else 0

    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setenv("HOSTED_WORKER_EXECUTE", "1")
    monkeypatch.setenv("HOSTED_WORKER_ALLOW_LIVE_APPLY", "1")
    monkeypatch.setenv("HOSTED_WORKER_ISOLATION_MODE", "container")
    monkeypatch.setenv("HOSTED_WORKER_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(hosted_worker.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(hosted_worker.time, "sleep", lambda _seconds: None)

    assert run_once(queue) is True
    assert queue.get(run.id).status == "succeeded"


def test_hosted_api_requires_token_and_enqueues_runs(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    from job_agent.hosted.auth import HostedIdentityStore
    identities = HostedIdentityStore(queue)
    token = identities.issue_key("u1")
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue, identities=identities)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = _request(f"{base}/health")
        assert status == 200
        assert payload["ok"] is True

        body = json.dumps({"user_id": "u1", "phases": ["source"], "options": {"dry_run": True}}).encode()
        status, payload = _request(f"{base}/runs", "POST", body=body)
        assert status == 401

        status, payload = _request(f"{base}/runs", "POST", token=token, body=body)
        assert status == 202
        run_id = payload["run"]["id"]
        assert queue.get(run_id).status == "pending"

        status, duplicate = _request(
            f"{base}/runs",
            "POST",
            token=token,
            body=body,
            headers={"Idempotency-Key": "api-retry-1"},
        )
        assert status == 202
        retry_body = json.dumps({"user_id": "u1", "phases": ["evaluate"], "options": {"dry_run": True}}).encode()
        status, retry = _request(
            f"{base}/runs",
            "POST",
            token=token,
            body=retry_body,
            headers={"Idempotency-Key": "api-retry-1"},
        )
        assert status == 202
        assert retry["run"]["id"] == duplicate["run"]["id"]
        assert queue.counts() == {"pending": 2}

        status, payload = _request(f"{base}/runs/{run_id}", token=token)
        assert status == 200
        assert payload["run"]["user_id"] == "u1"
        status, listed = _request(f"{base}/runs", token=token)
        assert status == 200
        assert all(run["user_id"] == "u1" for run in listed["runs"])
        assert run_id in {run["id"] for run in listed["runs"]}
        JobsDatabase().record_run_event(
            run_id=f"hosted:{run_id}",
            candidate_id="u1",
            phase="hosted_worker",
            event_type="validation_succeeded",
            success=True,
        )
        JobsDatabase().record_run_event(
            run_id=f"hosted:{run_id}",
            candidate_id="u2",
            phase="hosted_worker",
            event_type="wrong_user",
            success=True,
        )
        status, events = _request(f"{base}/runs/{run_id}/events", token=token)
        assert status == 200
        assert [event["event_type"] for event in events["events"]] == ["validation_succeeded"]
    finally:
        server.shutdown()
        server.server_close()


def test_hosted_jobs_are_paginated_and_user_scoped(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    from job_agent.hosted.auth import HostedIdentityStore
    identities = HostedIdentityStore(queue)
    token = identities.issue_key("u1")
    other = identities.issue_key("u2")
    for job_id in ("a", "b", "c"):
        identities.put_job("u1", job_id, {"id": job_id, "title": f"Job {job_id}"})
    identities.put_job("u2", "z", {"id": "z", "title": "Private"})
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue, identities=identities)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, first = _request(f"{base}/jobs?limit=2", token=token)
        assert status == 200
        assert [job["id"] for job in first["jobs"]] == ["a", "b"]
        assert first["next_cursor"] == "b"

        status, second = _request(f"{base}/jobs?limit=2&after=b", token=token)
        assert status == 200
        assert [job["id"] for job in second["jobs"]] == ["c"]
        assert second["next_cursor"] is None
        assert _request(f"{base}/jobs", token=other)[1]["jobs"] == [{"id": "z", "title": "Private"}]
    finally:
        server.shutdown()
        server.server_close()


def test_hosted_api_cors_allows_trusted_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_allowed_origins", "https://app.example.com")
    queue = HostedQueue(tmp_path / "hosted.db")
    from job_agent.hosted.auth import HostedIdentityStore
    identities = HostedIdentityStore(queue)
    token = identities.issue_key("u1")
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue, identities=identities)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload, headers = _request(
            f"{base}/jobs/stats",
            token=token,
            headers={"Origin": "https://app.example.com"},
            include_headers=True,
        )
        assert status == 200
        assert payload == {"jobs": 0}
        assert headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    finally:
        server.shutdown()
        server.server_close()


def test_hosted_api_rejects_untrusted_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_allowed_origins", "https://app.example.com")
    queue = HostedQueue(tmp_path / "hosted.db")
    from job_agent.hosted.auth import HostedIdentityStore
    identities = HostedIdentityStore(queue)
    token = identities.issue_key("u1")
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue, identities=identities)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = _request(f"{base}/jobs/stats", token=token, headers={"Origin": "https://evil.example"})
        assert status == 403
        assert payload["error"]["code"] == "origin_forbidden"
    finally:
        server.shutdown()
        server.server_close()


def test_hosted_api_options_preflight_for_trusted_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_allowed_origins", "https://app.example.com")
    queue = HostedQueue(tmp_path / "hosted.db")
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload, headers = _request(
            f"{base}/runs",
            method="OPTIONS",
            headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"},
            include_headers=True,
        )
        assert status == 204
        assert payload == {}
        assert headers["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert "POST" in headers["Access-Control-Allow-Methods"]
        assert "Authorization" in headers["Access-Control-Allow-Headers"]
        assert "Idempotency-Key" in headers["Access-Control-Allow-Headers"]
    finally:
        server.shutdown()
        server.server_close()


def test_hosted_api_rate_limits_authenticated_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_rate_limit_per_minute", 1)
    queue = HostedQueue(tmp_path / "hosted.db")
    from job_agent.hosted.auth import HostedIdentityStore
    identities = HostedIdentityStore(queue)
    token = identities.issue_key("u1")
    server = HostedApiServer(("127.0.0.1", 0), partial(HostedApiHandler), queue=queue, identities=identities)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _request(f"{base}/jobs/stats", token=token)[0] == 200
        status, payload = _request(f"{base}/jobs/stats", token=token)
        assert status == 429
        assert payload["error"]["code"] == "rate_limited"
    finally:
        server.shutdown()
        server.server_close()


def test_db_check_reports_sqlite_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)

    result = CliRunner().invoke(cli, ["db-check"])

    assert result.exit_code == 0
    assert "Backend" in result.output
    assert "sqlite" in result.output
    assert "hosted_runs" in result.output


def test_queue_status_and_recover_commands(tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    queue = HostedQueue()
    run = queue.enqueue("user-1", ["source"], {"dry_run": True})
    assert queue.claim_next().id == run.id
    with queue._connect() as conn:
        conn.execute(
            "UPDATE hosted_runs SET heartbeat_at = '2020-01-01T00:00:00+00:00', updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (run.id,),
        )

    runner = CliRunner()
    status = runner.invoke(cli, ["queue", "status"])
    assert status.exit_code == 0, status.output
    assert "running" in status.output

    recovered = runner.invoke(cli, ["queue", "recover-stale", "--older-than", "1"])
    assert recovered.exit_code == 0, recovered.output
    assert "Recovered" in recovered.output
    assert queue.get(run.id).status == "retryable"
