import json
from unittest.mock import Mock

import pytest
from tests.test_tailoring import candidate_profile  # noqa: F401  (fixture)
import requests

from job_agent.llm import GroqClient, LLMError
from job_agent.config.settings import Settings


def response(status=200, content='{"ok":true}', finish="stop", headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = json.dumps({"choices": [{"finish_reason": finish,
                                             "message": {"content": content}}]}).encode()
    result._content_consumed = True
    return result


def test_invalid_key_fails_over_without_disclosing_secrets():
    session = Mock()
    session.post.side_effect = [response(401), response(), response()]
    client = GroqClient(["secret-one", "secret-two"], "test-model", session=session)
    assert client.complete([], json_mode=True) == '{"ok":true}'
    client.complete([])
    assert session.post.call_count == 3
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-two"
    assert session.post.call_args.kwargs["allow_redirects"] is False


def test_rate_limit_on_one_key_rotates_to_the_next_key_instead_of_blocking_everything():
    """Each configured key is its own account, so a 429 on key one must not stop
    key two from being tried in the very same call."""
    session = Mock()
    session.post.side_effect = [response(429, headers={"retry-after": "120"}), response()]
    client = GroqClient(["one", "two"], "test", session=session)
    assert client.complete([], json_mode=True) == '{"ok":true}'
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer two"
    # Key "one" is still cooling down; the next call should skip straight to "two" again.
    session.post.side_effect = [response()]
    assert client.complete([], json_mode=True) == '{"ok":true}'
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer two"


def test_all_keys_rate_limited_raises_naming_the_soonest_cooldown():
    session = Mock()
    session.post.side_effect = [response(429, headers={"retry-after": "120"}),
                                 response(429, headers={"retry-after": "90"})]
    client = GroqClient(["one", "two"], "test", session=session)
    with pytest.raises(LLMError, match="retry after 90s"):
        client.complete([])
    assert session.post.call_count == 2


@pytest.mark.parametrize("reply", [response(content="not json"), response(finish="length"),
                                    response(content="[]"), response(content="")])
def test_invalid_or_truncated_outputs_are_not_accepted(reply):
    session = Mock()
    session.post.return_value = reply
    with pytest.raises(LLMError, match="incomplete or invalid"):
        GroqClient(["secret"], "test", session=session).complete([], json_mode=True)


def test_transient_failure_is_bounded_and_errors_are_redacted():
    session = Mock()
    session.post.side_effect = requests.ConnectionError("sensitive-secret")
    with pytest.raises(LLMError) as error:
        GroqClient(["first", "second"], "test", session=session).complete([])
    assert "sensitive-secret" not in str(error.value)
    assert session.post.call_count == 2


def test_bad_request_does_not_burn_through_keys():
    session = Mock()
    session.post.return_value = response(400)
    with pytest.raises(LLMError, match="HTTP 400"):
        GroqClient(["first", "second"], "test", session=session).complete([])
    assert session.post.call_count == 1


def test_groq_configuration_keeps_keys_out_of_repr():
    config = Settings(_env_file=None, GROQ_API_KEYS="test-one,test-two,test-one",
                      GROQ_API_KEY="test-three", DEFAULT_LLM_PROVIDER="groq")
    assert config.groq_keys == ["test-three", "test-one", "test-two"]
    assert config.active_provider == "groq"
    assert "test-one" not in repr(config)


def daily_limit_response(model="big", used=197271):
    result = requests.Response()
    result.status_code = 429
    result.headers.update({"retry-after": "196"})
    result._content = json.dumps({"error": {"message": (
        f"Rate limit reached for model `{model}` in organization `org_x` service tier `on_demand` on tokens "
        f"per day (TPD): Limit 200000, Used {used}, Requested 3181. Please try again in 3m15s.")}}).encode()
    result._content_consumed = True
    return result


def test_daily_token_limit_moves_to_the_fallback_model_and_stays_there():
    session = Mock()
    session.post.side_effect = [daily_limit_response(), response(), response()]
    client = GroqClient(["one"], "big", session=session, fallback_model="small")
    assert client.complete([], json_mode=True) == '{"ok":true}'
    assert [c.kwargs["json"]["model"] for c in session.post.call_args_list] == ["big", "small"]
    client.complete([])
    assert session.post.call_args.kwargs["json"]["model"] == "small"
    assert client.last_model == "small"


def test_daily_token_limit_without_a_fallback_says_what_happened():
    session = Mock()
    session.post.return_value = daily_limit_response()
    client = GroqClient(["one"], "big", session=session)
    with pytest.raises(LLMError, match="daily tokens limit reached for big: 197,271 of 200,000") as error:
        client.complete([])
    assert "GROQ_FALLBACK_MODEL" in str(error.value)
    assert "org_x" not in str(error.value)


def test_daily_limit_on_one_account_rotates_to_a_different_account_same_model():
    """The headline case: keys from separate Groq accounts, so one account's daily
    cap must not force a weaker fallback model when another account still has
    full quota on the primary model."""
    session = Mock()
    session.post.side_effect = [daily_limit_response("test", used=199999), response()]
    client = GroqClient(["one", "two"], "test", session=session)
    assert client.complete([], json_mode=True) == '{"ok":true}'
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["json"]["model"] == "test"
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer two"


def test_both_models_exhausted_stops_cleanly():
    session = Mock()
    session.post.side_effect = [daily_limit_response("big"), daily_limit_response("small")]
    client = GroqClient(["one"], "big", session=session, fallback_model="small")
    with pytest.raises(LLMError, match="daily tokens limit reached for small"):
        client.complete([])
    assert session.post.call_count == 2


def test_malformed_json_rejected_by_groq_is_resampled():
    bad = requests.Response()
    bad.status_code = 400
    bad._content = json.dumps({"error": {"code": "json_validate_failed", "message": "Failed to generate JSON"}}).encode()
    bad._content_consumed = True
    session = Mock()
    session.post.side_effect = [bad, response()]
    assert GroqClient(["one"], "m", session=session).complete([], json_mode=True) == '{"ok":true}'
    assert session.post.call_count == 2


def test_request_too_large_retries_with_a_smaller_output_allowance():
    # Groq counts prompt + reserved output against one per-request limit, so an
    # oversized max_tokens must shrink instead of failing the whole phase.
    session = Mock()
    session.post.side_effect = [response(413), response(413), response()]
    client = GroqClient(["one"], "test", session=session)
    assert client.complete([], json_mode=True, max_tokens=8000) == '{"ok":true}'
    sent = [call.kwargs["json"]["max_completion_tokens"] for call in session.post.call_args_list]
    assert sent == [8000, 4000, 2000]


def test_request_too_large_gives_an_actionable_error_not_a_model_access_hint():
    session = Mock()
    session.post.return_value = response(413)
    with pytest.raises(LLMError, match="too large") as error:
        GroqClient(["one"], "test", session=session).complete([], max_tokens=1500)
    assert "model access" not in str(error.value)
    assert session.post.call_count == 1


def test_a_score_is_labelled_with_the_model_that_actually_judged_it(monkeypatch, candidate_profile):
    """When Groq's daily limit moves the client to its fallback model, the label must follow."""
    from job_agent import llm
    from job_agent.config.schema import JobPosting
    from job_agent.evaluation.reranker import LLMReranker

    job = JobPosting(id="j1", title="AI Engineer", company="Acme", location="Remote", is_remote=True,
                     job_url="https://example.com/1", source="test", description="Build things in Python.")
    reranker = LLMReranker(provider="groq")
    monkeypatch.setattr(reranker, "_call_groq", lambda *a, **k: {
        "fit_score": 6.0, "technical_score": 6.0, "seniority_score": 6.0, "reasoning": "ok",
        "matching_skills": [], "missing_skills": []})

    class Client:
        last_model = "openai/gpt-oss-20b"

    monkeypatch.setattr(llm, "_client", Client())
    assert LLMReranker.evaluate_job(reranker, candidate_profile, job, 0.5).scored_by == "groq:openai/gpt-oss-20b"
    monkeypatch.setattr(llm, "_client", None)
    assert LLMReranker.evaluate_job(reranker, candidate_profile, job, 0.5).scored_by.startswith("groq:")


def tool_call_response(tool_name, arguments, call_id="call_1"):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps({"choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(arguments)}}],
        },
    }]}).encode()
    result._content_consumed = True
    return result


def test_groq_client_returns_the_tool_call_message_when_the_model_calls_a_tool():
    session = Mock()
    session.post.return_value = tool_call_response("get_profile_evidence", {"query": "python"})
    client = GroqClient(["secret"], "test-model", session=session)
    message = client.complete([], tools=[{"type": "function", "function": {"name": "get_profile_evidence"}}])
    assert message["tool_calls"][0]["function"]["name"] == "get_profile_evidence"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"query": "python"}
    assert session.post.call_args.kwargs["json"]["tool_choice"] == "auto"


def test_groq_complete_with_tools_executes_the_dispatcher_and_records_an_event(monkeypatch, tmp_path):
    from job_agent import llm
    from job_agent.config.settings import settings
    from job_agent.storage.jobs_db import JobsDatabase

    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "groq_api_key", Settings(_env_file=None).groq_api_key)
    monkeypatch.setattr(settings, "groq_api_keys", Settings(_env_file=None, GROQ_API_KEYS="one").groq_api_keys)
    monkeypatch.setattr(settings, "groq_model", "test-model")
    monkeypatch.setattr(settings, "groq_timeout", 45)
    monkeypatch.setattr(settings, "groq_fallback_model", "")
    session = Mock()
    session.post.side_effect = [
        tool_call_response("get_profile_evidence", {"query": "python", "max_results": 2}),
        response(content="done"),
    ]
    monkeypatch.setattr(llm, "_client", GroqClient(["one"], "test-model", session=session))
    monkeypatch.setattr(llm, "_configuration", (("one",), "test-model", 45, ""))

    captured = []

    def dispatch(name, arguments):
        captured.append((name, arguments))
        return {"results": [{"statement": "Shipped a thing"}]}

    text, calls = llm.groq_complete_with_tools(
        "system", "prompt", tools=[{"type": "function", "function": {"name": "get_profile_evidence"}}],
        dispatch=dispatch)

    assert text == "done"
    assert calls == [("get_profile_evidence", {"query": "python", "max_results": 2},
                      {"results": [{"statement": "Shipped a thing"}]})]
    assert captured == [("get_profile_evidence", {"query": "python", "max_results": 2})]
    assert session.post.call_count == 2
    second_messages = session.post.call_args_list[1].kwargs["json"]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert json.loads(second_messages[-1]["content"]) == {"results": [{"statement": "Shipped a thing"}]}

    event = JobsDatabase().run_events(phase="llm", limit=1)[0]
    assert event["event_type"] == "groq_tool_call"
    assert event["success"] is True
    assert event["metadata"]["tool_calls"] == 1
    assert event["metadata"]["tool_names"] == ["get_profile_evidence"]


def test_groq_complete_with_tools_stops_after_max_rounds_without_a_final_answer(monkeypatch, tmp_path):
    from job_agent import llm
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "groq_api_key", Settings(_env_file=None).groq_api_key)
    monkeypatch.setattr(settings, "groq_api_keys", Settings(_env_file=None, GROQ_API_KEYS="one").groq_api_keys)
    monkeypatch.setattr(settings, "groq_model", "test-model")
    monkeypatch.setattr(settings, "groq_timeout", 45)
    monkeypatch.setattr(settings, "groq_fallback_model", "")
    session = Mock()
    session.post.return_value = tool_call_response("get_profile_evidence", {"query": "python"})
    monkeypatch.setattr(llm, "_client", GroqClient(["one"], "test-model", session=session))
    monkeypatch.setattr(llm, "_configuration", (("one",), "test-model", 45, ""))

    text, calls = llm.groq_complete_with_tools(
        "system", "prompt", tools=[{"type": "function", "function": {"name": "get_profile_evidence"}}],
        dispatch=lambda name, args: {"results": []}, max_rounds=2)

    assert text is None
    assert len(calls) == 2
    assert session.post.call_count == 2


def test_groq_complete_records_safe_llm_metadata(monkeypatch, tmp_path):
    from job_agent import llm
    from job_agent.config.settings import settings
    from job_agent.storage.jobs_db import JobsDatabase

    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "groq_api_key", Settings(_env_file=None).groq_api_key)
    monkeypatch.setattr(settings, "groq_api_keys", Settings(_env_file=None, GROQ_API_KEYS="one").groq_api_keys)
    monkeypatch.setattr(settings, "groq_model", "test-model")
    monkeypatch.setattr(settings, "groq_timeout", 45)
    monkeypatch.setattr(settings, "groq_fallback_model", "")
    session = Mock()
    session.post.return_value = response(content='{"ok": true}')
    monkeypatch.setattr(llm, "_client", GroqClient(["one"], "test-model", session=session))
    monkeypatch.setattr(llm, "_configuration", (("one",), "test-model", 45, ""))

    assert llm.groq_complete("System secret", "Candidate private prompt") == {"ok": True}

    event = JobsDatabase().run_events(phase="llm", limit=1)[0]
    assert event["event_type"] == "groq_complete"
    assert event["success"] is True
    assert event["metadata"]["provider"] == "groq"
    assert event["metadata"]["model"] == "test-model"
    assert "prompt_hash" in event["metadata"]
    assert "response_hash" in event["metadata"]
    assert "Candidate private prompt" not in json.dumps(event["metadata"])
