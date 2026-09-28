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
