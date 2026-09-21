"""Ordinary tests never consume the user's API quota or candidate artifacts."""
import pytest

from job_agent.config.settings import settings


@pytest.fixture(autouse=True)
def isolate_llm_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "default_llm_provider", "none")
    monkeypatch.setattr(settings, "llm_strict", False)
    monkeypatch.setattr(settings, "llama_cloud_api_key", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "outputs")
