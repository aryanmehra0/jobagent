import json
from unittest.mock import Mock

import pytest
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


def test_organization_rate_limit_stops_rotation_and_cools_down():
    session = Mock()
    session.post.return_value = response(429, headers={"retry-after": "120"})
    client = GroqClient(["one", "two"], "test", session=session)
    with pytest.raises(LLMError, match="120s"):
        client.complete([])
    with pytest.raises(LLMError, match="organization quota"):
        client.complete([])
    assert session.post.call_count == 1


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
