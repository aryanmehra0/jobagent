"""Phase 7 evidence selection via real provider tool-calling, not a JSON-prompt trick."""
from job_agent.config.schema import JobPosting
from job_agent.config.settings import Settings
from job_agent.generation import evidence_for, _evidence_tool_dispatcher, _evidence_tool_spec
from tests.test_tailoring import candidate_profile  # noqa: F401  (fixture)


def _activate_groq(monkeypatch, settings):
    """Point active_provider at groq the way a real .env would, without a live key."""
    monkeypatch.setattr(settings, "default_llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", Settings(_env_file=None).groq_api_key)
    monkeypatch.setattr(settings, "groq_api_keys", Settings(_env_file=None, GROQ_API_KEYS="test-key").groq_api_keys)


def _job(description="Looking for someone with Python and team leadership experience."):
    return JobPosting(id="j1", title="AI Engineer", company="Acme", location="Remote", is_remote=True,
                      job_url="https://example.com/1", source="test", description=description)


def test_dispatcher_only_ever_returns_entries_already_in_the_profile():
    entries = [{"statement": "Built an ML pipeline in Python", "company": "Acme", "title": "Engineer"},
               {"statement": "Led a team of five", "company": "Acme", "title": "Engineer"}]
    dispatch = _evidence_tool_dispatcher(entries)
    result = dispatch("get_profile_evidence", {"query": "python pipeline", "max_results": 1})
    assert result["results"] == [entries[0]]
    assert dispatch("unknown_tool", {}) == {"error": "unknown tool: unknown_tool"}


def test_tool_spec_declares_the_function_the_model_can_call():
    spec = _evidence_tool_spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "get_profile_evidence"
    assert "query" in spec["function"]["parameters"]["properties"]


def test_evidence_for_calls_the_real_groq_tool_calling_path(monkeypatch, candidate_profile):
    from job_agent.config.settings import settings
    from job_agent import llm

    _activate_groq(monkeypatch, settings)
    assert settings.active_provider == "groq"

    seen_tools = []

    def fake_tool_call(system, prompt, *, tools, dispatch, max_tokens=1200, max_rounds=3):
        seen_tools.append(tools)
        result = dispatch("get_profile_evidence", {"query": "python", "max_results": 1})
        return "ok", [("get_profile_evidence", {"query": "python", "max_results": 1}, result)]

    monkeypatch.setattr(llm, "groq_complete_with_tools", fake_tool_call)

    evidence = evidence_for(candidate_profile, _job())

    assert seen_tools and seen_tools[0][0]["function"]["name"] == "get_profile_evidence"
    assert evidence
    baseline_statements = {e["statement"] for e in evidence_for(candidate_profile, _job(), use_llm=False)}
    assert {e["statement"] for e in evidence} == baseline_statements


def test_evidence_for_falls_back_to_the_deterministic_sort_when_the_tool_call_fails(monkeypatch, candidate_profile):
    from job_agent.config.settings import settings
    from job_agent import llm

    _activate_groq(monkeypatch, settings)

    def boom(*args, **kwargs):
        raise RuntimeError("groq is unreachable")

    monkeypatch.setattr(llm, "groq_complete_with_tools", boom)

    job = _job()
    baseline = evidence_for(candidate_profile, job, use_llm=False)
    evidence = evidence_for(candidate_profile, job)
    assert evidence == baseline
