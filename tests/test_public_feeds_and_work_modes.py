"""Coverage for explicit work modes and public API ingestion."""

from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.sourcing.public_feeds import PublicFeedIngestion
from job_agent.sourcing.scraper import OmnichannelScraper


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def _job(mode: str) -> JobPosting:
    return JobPosting(
        id=f"mode-{mode}",
        title="AI Product Manager",
        company="Acme",
        location="Bengaluru, India" if mode != "remote" else "Remote - Worldwide",
        job_url=f"https://example.com/{mode}",
        source="indeed",
        work_mode=mode,
        is_remote=mode == "remote",
    )


def test_explicit_work_modes_are_independent_choices():
    scraper = OmnichannelScraper(SearchParameters(
        target_domains=["AI Product Manager"],
        work_modes=["hybrid", "onsite"],
        onsite_countries=["india"],
        is_remote=False,
    ))
    assert scraper._passes_filters(_job("hybrid"))
    assert scraper._passes_filters(_job("onsite"))
    assert not scraper._passes_filters(_job("remote"))


def test_job_posting_infers_hybrid_before_remote():
    job = JobPosting(
        id="hybrid-infer",
        title="Product Manager (Hybrid)",
        company="Acme",
        location="Hybrid - Mumbai, India",
        job_url="https://example.com/hybrid",
        source="naukri",
    )
    assert job.work_mode == "hybrid"
    assert job.is_remote is False


def test_remotive_uses_one_polite_request_and_normalizes_results():
    session = _Session({"jobs": [
        {
            "title": "AI Product Manager",
            "company_name": "Example AI",
            "url": "https://remotive.com/remote-jobs/product/ai-product-manager-1",
            "candidate_required_location": "Worldwide",
            "publication_date": "2026-09-17T00:00:00Z",
            "job_type": "full_time",
            "description": "<p>Build AI products. Contact careers@example.ai</p>",
        },
        {
            "title": "Account Executive",
            "company_name": "Sales Co",
            "url": "https://remotive.com/remote-jobs/sales/ae-2",
            "description": "Sales",
        },
    ]})
    feed = PublicFeedIngestion(session=session)
    jobs = feed.fetch_remotive(SearchParameters(
        target_domains=["AI Product Manager"], public_sources=["remotive"]
    ))
    assert len(session.calls) == 1
    assert len(jobs) == 1
    assert jobs[0].source == "remotive"
    assert jobs[0].work_mode == "remote"
    assert jobs[0].primary_contact().email == "careers@example.ai"


def test_remote_country_restriction_respects_candidate_preference():
    from job_agent.config.schema import CandidateProfile, ContactInfo, SkillSet, WorkAuthorization

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Candidate", email="candidate@example.com"),
        work_authorization=WorkAuthorization(
            current_country="India", authorized_countries=["India"], remote_worldwide=False
        ),
        skills=SkillSet(),
        summary="Product professional.",
        years_of_experience=3,
    ).seal_profile()
    scraper = OmnichannelScraper(
        SearchParameters(target_domains=["AI Product Manager"], work_modes=["remote"]),
        candidate_profile=profile,
    )
    foreign = _job("remote").model_copy(update={"location": "Remote - United States"})
    worldwide = _job("remote")
    assert not scraper._passes_filters(foreign)
    assert scraper._passes_filters(worldwide)


def test_jobicy_preserves_country_restrictions_and_published_contacts():
    session = _Session({"jobs": [{
        "jobTitle": "AI Product Manager", "companyName": "Example", "jobGeo": "USA",
        "url": "https://jobicy.com/jobs/123", "pubDate": "2026-09-18 08:00:00",
        "jobDescription": "<p>Contact careers@example.ai</p>",
    }]})
    feed = PublicFeedIngestion(session=session)
    params = SearchParameters(target_domains=["AI Product Manager"], public_sources=["jobicy"])
    first = feed.scrape_configured(params)
    second = feed.scrape_configured(params)
    assert len(first) == len(second) == 1
    assert first[0].location == "USA"
    assert first[0].primary_contact().email == "careers@example.ai"
    assert len(session.calls) == 1
    assert feed.report["jobicy"]["cached_requests"] == 1


def test_malformed_feed_is_failure_not_successful_empty_search():
    feed = PublicFeedIngestion(session=_Session({"error": "unavailable"}))
    assert not feed.scrape_configured(SearchParameters(public_sources=["jobicy"]))
    assert feed.report["jobicy"]["status"] == "failed"


def test_cancellation_is_not_swallowed_by_public_feed(monkeypatch):
    import pytest
    from job_agent.runtime import RunCancelled
    feed = PublicFeedIngestion(session=_Session({}))
    def stop(params):
        raise RunCancelled("Stopped")
    monkeypatch.setattr(feed, "fetch_jobicy", stop)
    with pytest.raises(RunCancelled):
        feed.scrape_configured(SearchParameters(public_sources=["jobicy"]))
