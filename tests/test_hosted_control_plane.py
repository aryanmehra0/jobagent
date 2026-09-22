"""Hosted API and queue smoke tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial

from click.testing import CliRunner

from job_agent.cli import cli
from job_agent.hosted.api import HostedApiHandler, HostedApiServer
from job_agent.hosted.queue import HostedQueue
from job_agent.hosted.worker import run_once


def _request(url: str, method: str = "GET", token: str = "", body: bytes | None = None):
    request = urllib.request.Request(url, method=method, data=body)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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

    queue.finish(run.id, ok=True)
    assert queue.get(run.id).status == "succeeded"


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
    assert run_once(queue) is False


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

        status, payload = _request(f"{base}/runs/{run_id}", token=token)
        assert status == 200
        assert payload["run"]["user_id"] == "u1"
    finally:
        server.shutdown()
        server.server_close()


def test_db_check_reports_sqlite_fallback(tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)

    result = CliRunner().invoke(cli, ["db-check"])

    assert result.exit_code == 0
    assert "Backend" in result.output
    assert "sqlite" in result.output
    assert "hosted_runs" in result.output
