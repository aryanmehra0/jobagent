"""Ordinary tests never consume the user's API quota or candidate artifacts."""
import pytest

from job_agent.config.settings import settings


@pytest.fixture(autouse=True)
def isolate_llm_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "default_llm_provider", "none")
    monkeypatch.setattr(settings, "llm_strict", False)
    monkeypatch.setattr(settings, "llama_cloud_api_key", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "outputs")
    # A developer's real .env may have a dashboard login configured (this
    # session's did) -- without resetting these, plain `pytest` (no
    # JOB_AGENT_LOAD_DOTENV=0) loads it, and every dashboard test that doesn't
    # send Basic Auth starts failing with 401 for reasons invisible from the
    # test file itself. Tests that specifically exercise the login set these
    # themselves via monkeypatch (see tests/test_web.py's auth_console fixture).
    monkeypatch.setattr(settings, "dashboard_username", None)
    monkeypatch.setattr(settings, "dashboard_password", None)
    monkeypatch.setattr(settings, "dashboard_allowed_hosts", None)
    # Every other Settings field a real .env can set and a test could silently inherit:
    # a configured DATABASE_URL would send tests to the operator's real database, and a
    # tuned MIN_MATCH_SCORE / TIER1_THRESHOLD / TAILORING_MODE changes what "qualifies".
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "min_match_score", 7.0)
    monkeypatch.setattr(settings, "tier1_threshold", None)
    monkeypatch.setattr(settings, "tailoring_mode", "auto")
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    monkeypatch.setattr(settings, "raw_resumes_dir", tmp_path / "raw_resumes")
    # No test may call out to a resolver: the SSRF guard is exercised explicitly in test_netguard.py.
    import job_agent.contacts.finder as finder

    monkeypatch.setattr(finder, "_resolves_to_public_address", lambda host, port: True)
