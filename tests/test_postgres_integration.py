"""Opt-in PostgreSQL integration checks for the normalized jobs backend.

Run with a disposable database URL:

    $env:JOB_AGENT_POSTGRES_TEST_URL='postgresql://user:pass@localhost:5432/job_agent_test'
    python -m pytest tests/test_postgres_integration.py -q

The test creates and drops its own schema inside that database.
"""

from __future__ import annotations

import json
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.storage.jobs_db import JobsDatabase


def _schema_url(base_url: str, schema: str) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def test_postgres_jobs_and_delta_store_share_one_database(monkeypatch, tmp_path):
    base_url = os.environ.get("JOB_AGENT_POSTGRES_TEST_URL")
    if not base_url:
        pytest.skip("Set JOB_AGENT_POSTGRES_TEST_URL to run the real Postgres integration test.")

    psycopg = pytest.importorskip("psycopg")
    schema = "job_agent_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    database_url = _schema_url(base_url, schema)
    try:
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps({
            "contact": {"full_name": "Asha Verma", "email": "asha@example.org"},
            "summary": "Product manager.",
            "work_authorization": {"current_country": "India", "authorized_countries": ["India"]},
            "skills": {"languages": ["Python"]},
            "profile_hash": "profile-v1",
        }), encoding="utf-8")
        monkeypatch.setattr(settings, "outputs_dir", outputs)
        monkeypatch.setattr(settings, "profile_path", profile)
        monkeypatch.setattr(settings, "database_url", database_url)

        job = JobPosting(
            id="j1", title="Associate Product Manager", company="Osfin.ai",
            location="Bengaluru, India", job_url="https://example.com/jobs/1",
            source="greenhouse", description="Build product workflows with Python.",
            work_mode="hybrid",
        )
        evaluation = {
            "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
            "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
            "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
            "missing_skills": [], "scored_by": "heuristic",
            "evaluated_at": "2026-09-18T10:00:00Z",
        }
        (outputs / "scraped_jobs.json").write_text(json.dumps([job.model_dump()]), encoding="utf-8")
        (outputs / "evaluated_jobs.json").write_text(
            json.dumps([{"job": job.model_dump(), "evaluation": evaluation}]), encoding="utf-8"
        )
        (outputs / "qualified_jobs.json").write_text(
            json.dumps([{"job": job.model_dump(), "evaluation": evaluation}]), encoding="utf-8"
        )

        database = JobsDatabase(database_url=database_url)
        stats = database.sync(outputs)
        store = DeltaStore()
        store.mark_seen(job)

        assert stats["jobs"] == 1
        assert database.evaluated_jobs()[0].job.id == "j1"
        assert store.statuses(["j1"]) == {"j1": "scraped"}
        with database._connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM job_matches").fetchone()["n"] == 1
            assert conn.execute("SELECT COUNT(*) AS n FROM seen_jobs").fetchone()["n"] == 1
    finally:
        with psycopg.connect(base_url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
