"""Groq's per-minute token budget: wait out a shortfall instead of triggering a 429."""
import json
from unittest.mock import Mock

import pytest
import requests

from job_agent.llm import GroqClient


def response(headers=None):
    result = requests.Response()
    result.status_code = 200
    result.headers.update(headers or {})
    result._content = json.dumps({"choices": [{"finish_reason": "stop",
                                             "message": {"content": '{"ok":true}'}}]}).encode()
    result._content_consumed = True
    return result


@pytest.mark.parametrize("text,seconds", [("58.612s", 58.612), ("1m2.5s", 62.5), ("2m", 120.0), ("250ms", 0.25)])
def test_reset_header_durations_are_understood(text, seconds):
    assert GroqClient._parse_duration(text) == pytest.approx(seconds)


def test_unreadable_reset_header_is_ignored():
    assert GroqClient._parse_duration(None) is None
    assert GroqClient._parse_duration("soon") is None


def test_the_client_waits_out_a_token_shortfall_instead_of_triggering_a_429(monkeypatch):
    # Groq reports how many tokens of the per-minute budget remain. A request that
    # would not fit is held for exactly the refill time, rather than being sent,
    # rejected, and then sleeping a full cooldown.
    waits = []
    monkeypatch.setattr("job_agent.llm.time.sleep", lambda seconds: waits.append(seconds))
    session = Mock()
    session.post.return_value = response({"x-ratelimit-remaining-tokens": "200",
                                          "x-ratelimit-limit-tokens": "8000"})
    client = GroqClient(["one"], "test", session=session)
    client.complete([{"role": "user", "content": "x" * 700}], max_tokens=1000)
    assert waits == []                        # nothing known before the first reply
    client.complete([{"role": "user", "content": "x" * 7000}], max_tokens=1000)
    assert len(waits) == 1 and 15 < waits[0] < 25, waits
    assert session.post.call_count == 2       # one request each: no 429 round trip


def test_no_wait_when_the_budget_is_ample(monkeypatch):
    waits = []
    monkeypatch.setattr("job_agent.llm.time.sleep", lambda seconds: waits.append(seconds))
    session = Mock()
    session.post.return_value = response({"x-ratelimit-remaining-tokens": "7900",
                                          "x-ratelimit-limit-tokens": "8000"})
    client = GroqClient(["one"], "test", session=session)
    client.complete([{"role": "user", "content": "hello"}], max_tokens=500)
    client.complete([{"role": "user", "content": "hello again"}], max_tokens=500)
    assert waits == []


def test_a_request_that_can_never_fit_is_let_through_for_groq_to_answer(monkeypatch):
    waits = []
    monkeypatch.setattr("job_agent.llm.time.sleep", lambda seconds: waits.append(seconds))
    session = Mock()
    session.post.return_value = response({"x-ratelimit-remaining-tokens": "10",
                                          "x-ratelimit-limit-tokens": "8000"})
    client = GroqClient(["one"], "test", session=session)
    client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
    client.complete([{"role": "user", "content": "x" * 40000}], max_tokens=1000)   # larger than the whole budget
    assert waits == []
