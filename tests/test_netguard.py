"""Phase 4 / H1: untrusted URLs must not reach this machine's own network, even via redirects."""
from __future__ import annotations

import pytest

from job_agent import netguard
from job_agent.netguard import is_public_url, resolves_to_public_address, safe_get


class _Response:
    def __init__(self, status=200, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.is_redirect = bool(location) and status in (301, 302, 303, 307, 308)
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, routes):
        self.routes, self.requested, self.kwargs, self.headers = routes, [], [], {}

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.kwargs.append(kwargs)
        return self.routes[url]


PUBLIC = {"example.com", "cdn.example.com"}


def _check(host, port):
    return host in PUBLIC


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "::1"])
def test_internal_addresses_are_not_public(monkeypatch, address):
    monkeypatch.setattr(netguard.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (address, port))])
    assert resolves_to_public_address("whatever.example", 80) is False


def test_a_name_that_fails_to_resolve_is_refused(monkeypatch):
    def boom(host, port):
        raise OSError("no such host")
    monkeypatch.setattr(netguard.socket, "getaddrinfo", boom)
    assert resolves_to_public_address("nope.invalid", 80) is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http:///nohost", "gopher://example.com"])
def test_only_http_and_https_with_a_host_are_fetched(url):
    assert is_public_url(url, _check) is False


def test_a_public_page_that_redirects_to_localhost_is_not_followed():
    session = _Session({
        "https://example.com/a": _Response(302, "http://127.0.0.1:8765/api/state"),
        "http://127.0.0.1:8765/api/state": _Response(200),
    })
    assert safe_get(session, "https://example.com/a", check=_check) is None
    assert session.requested == ["https://example.com/a"], "the internal address must never be requested"


def test_a_public_redirect_chain_is_followed_and_never_auto_redirected():
    final = _Response(200)
    session = _Session({
        "https://example.com/a": _Response(301, "https://cdn.example.com/b"),
        "https://cdn.example.com/b": _Response(302, "/c"),   # relative hop
        "https://cdn.example.com/c": final,
    })
    assert safe_get(session, "https://example.com/a", check=_check, timeout=5) is final
    assert all(call["allow_redirects"] is False for call in session.kwargs)
    assert all(call["timeout"] == 5 for call in session.kwargs)


def test_a_redirect_loop_is_cut_off():
    session = _Session({"https://example.com/a": _Response(302, "https://example.com/a")})
    assert safe_get(session, "https://example.com/a", check=_check, max_redirects=3) is None
    assert len(session.requested) == 4


def test_the_company_crawler_will_not_follow_a_redirect_into_the_local_network(monkeypatch):
    from job_agent.contacts import finder

    monkeypatch.setattr(finder, "_resolves_to_public_address", lambda host, port: host == "acme.example")
    session = _Session({
        "https://acme.example/contact": _Response(302, "http://192.168.1.1/admin"),
        "http://192.168.1.1/admin": _Response(200),
    })
    crawler = finder.CompanySiteCrawler(session=session)
    assert crawler._get("https://acme.example/contact") is None
    assert "http://192.168.1.1/admin" not in session.requested


def test_the_description_fetcher_will_not_follow_a_redirect_into_the_local_network(monkeypatch):
    from job_agent.config.schema import JobPosting
    from job_agent.sourcing import details

    monkeypatch.setattr(details, "_resolves_to_public_address", lambda host, port: host == "jobs.example")
    session = _Session({
        "https://jobs.example/1": _Response(302, "http://127.0.0.1:8765/api/state"),
        "http://127.0.0.1:8765/api/state": _Response(200),
    })
    job = JobPosting(id="j1", title="AI Engineer", company="Acme", job_url="https://jobs.example/1", source="test")
    assert details._fetch_description(job, session) == ""
    assert session.requested == ["https://jobs.example/1"]
