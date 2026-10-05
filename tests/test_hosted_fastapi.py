"""Production hosted API tests for the FastAPI control plane."""

from __future__ import annotations

from fastapi.testclient import TestClient

from job_agent.config.settings import settings
from job_agent.hosted.auth import HostedIdentityStore
from job_agent.hosted.fastapi_app import create_app
from job_agent.hosted.queue import HostedQueue
from job_agent.storage.jobs_db import JobsDatabase


def _client(tmp_path):
    queue = HostedQueue(tmp_path / "hosted.db")
    identities = HostedIdentityStore(queue)
    app = create_app(queue=queue, identities=identities)
    return TestClient(app), queue, identities


def test_fastapi_openapi_and_health_are_available(tmp_path):
    client, _, _ = _client(tmp_path)

    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.headers["X-Request-ID"]

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/v1/runs" in schema.json()["paths"]


def test_fastapi_auth_enqueues_and_reads_only_owner_run(tmp_path):
    client, queue, identities = _client(tmp_path)
    token_a = identities.issue_key("user-a")
    token_b = identities.issue_key("user-b")

    created = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"phases": ["source"], "options": {"dry_run": True}},
    )
    assert created.status_code == 202
    run_id = created.json()["run"]["id"]
    assert queue.get(run_id).user_id == "user-a"

    owner = client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token_a}"})
    assert owner.status_code == 200
    assert owner.json()["run"]["user_id"] == "user-a"

    other = client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"


def test_fastapi_lists_only_owner_runs(tmp_path):
    client, queue, identities = _client(tmp_path)
    token_a = identities.issue_key("user-a")
    token_b = identities.issue_key("user-b")
    first = queue.enqueue("user-a", ["source"], {"dry_run": True})
    second = queue.enqueue("user-a", ["track"], {"dry_run": True})
    other = queue.enqueue("user-b", ["prep"], {"dry_run": True})
    claimed = queue.claim_next()
    assert claimed.id == first.id
    queue.finish(first.id, ok=True)

    owner = client.get("/v1/runs", headers={"Authorization": f"Bearer {token_a}"})
    assert owner.status_code == 200
    assert [run["id"] for run in owner.json()["runs"]] == [second.id, first.id]

    succeeded = client.get("/v1/runs?status=succeeded", headers={"Authorization": f"Bearer {token_a}"})
    assert succeeded.status_code == 200
    assert [run["id"] for run in succeeded.json()["runs"]] == [first.id]

    other_user = client.get("/v1/runs", headers={"Authorization": f"Bearer {token_b}"})
    assert [run["id"] for run in other_user.json()["runs"]] == [other.id]


def test_fastapi_run_events_are_owner_scoped(tmp_path):
    client, queue, identities = _client(tmp_path)
    token_a = identities.issue_key("user-a")
    token_b = identities.issue_key("user-b")
    created = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"phases": ["track"], "options": {"dry_run": True}},
    )
    run_id = created.json()["run"]["id"]
    database = JobsDatabase()
    database.record_run_event(
        run_id=f"hosted:{run_id}",
        candidate_id="user-a",
        phase="hosted_worker",
        event_type="validation_succeeded",
        success=True,
    )
    database.record_run_event(
        run_id=f"hosted:{run_id}",
        candidate_id="user-b",
        phase="hosted_worker",
        event_type="wrong_user",
        success=True,
    )

    owner = client.get(f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {token_a}"})
    assert owner.status_code == 200
    assert [event["event_type"] for event in owner.json()["events"]] == ["validation_succeeded"]

    other = client.get(f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {token_b}"})
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"


def test_fastapi_idempotency_key_returns_existing_run(tmp_path):
    client, queue, identities = _client(tmp_path)
    token_a = identities.issue_key("user-a")
    token_b = identities.issue_key("user-b")
    headers_a = {"Authorization": f"Bearer {token_a}", "Idempotency-Key": "retry-1"}

    first = client.post(
        "/v1/runs",
        headers=headers_a,
        json={"phases": ["source"], "options": {"dry_run": True}},
    )
    retry = client.post(
        "/v1/runs",
        headers=headers_a,
        json={"phases": ["evaluate"], "options": {"dry_run": True}},
    )
    other_user = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "retry-1"},
        json={"phases": ["source"], "options": {"dry_run": True}},
    )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json()["run"]["id"] == first.json()["run"]["id"]
    assert retry.json()["run"]["phases"] == ["source"]
    assert other_user.status_code == 202
    assert other_user.json()["run"]["id"] != first.json()["run"]["id"]
    assert queue.counts() == {"pending": 2}


def test_fastapi_rejects_unowned_user_id_and_invalid_payload(tmp_path):
    client, _, identities = _client(tmp_path)
    token = identities.issue_key("user-a")

    forbidden = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": "user-b", "phases": ["source"], "options": {"dry_run": True}},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    invalid = client.post("/v1/runs", headers={"Authorization": f"Bearer {token}"}, json={"phases": []})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"


def test_fastapi_jobs_are_user_scoped_and_cursor_paginated(tmp_path):
    client, _, identities = _client(tmp_path)
    token_a = identities.issue_key("user-a")
    token_b = identities.issue_key("user-b")
    for job_id in ("a", "b", "c"):
        identities.put_job("user-a", job_id, {"id": job_id, "title": f"Job {job_id}"})
    identities.put_job("user-b", "z", {"id": "z", "title": "Private"})

    first = client.get("/v1/jobs?limit=2", headers={"Authorization": f"Bearer {token_a}"})
    assert first.status_code == 200
    assert [job["id"] for job in first.json()["jobs"]] == ["a", "b"]
    assert first.json()["next_cursor"] == "b"

    second = client.get("/v1/jobs?limit=2&after=b", headers={"Authorization": f"Bearer {token_a}"})
    assert second.status_code == 200
    assert [job["id"] for job in second.json()["jobs"]] == ["c"]
    assert second.json()["next_cursor"] is None

    other = client.get("/v1/jobs", headers={"Authorization": f"Bearer {token_b}"})
    assert other.json()["jobs"] == [{"id": "z", "title": "Private"}]


def test_fastapi_cors_rejects_untrusted_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_allowed_origins", "https://app.example.com")
    client, _, identities = _client(tmp_path)
    token = identities.issue_key("user-a")

    allowed = client.get(
        "/v1/jobs/stats",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://app.example.com"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"

    rejected = client.get(
        "/v1/jobs/stats",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "origin_forbidden"


def test_fastapi_rate_limits_authenticated_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hosted_api_rate_limit_per_minute", 1)
    client, _, identities = _client(tmp_path)
    token = identities.issue_key("user-a")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/v1/jobs/stats", headers=headers).status_code == 200
    limited = client.get("/v1/jobs/stats", headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
