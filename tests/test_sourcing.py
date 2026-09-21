"""Unit and integration tests for Phase 2: Omnichannel Sourcing."""

from pathlib import Path
import pytest
import pandas as pd

from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.sourcing.proxy_manager import ProxyManager
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.sourcing.scraper import OmnichannelScraper
from job_agent.sourcing.ats_direct import ATSDirectIngestion


def test_job_posting_creation_and_deterministic_id():
    """Verify JobPosting creation and ID determinism."""
    url = "https://example.com/jobs/12345"
    id1 = JobPosting.create_id(url, "ScaleTech", "Staff Engineer")
    id2 = JobPosting.create_id(url, "ScaleTech", "Staff Engineer")
    assert id1 == id2
    assert len(id1) == 16

    posting = JobPosting(
        id=id1,
        title="Staff Engineer",
        company="ScaleTech",
        location="Remote",
        job_url=url,
        description="Lead distributed cloud architecture.",
        is_remote=True,
        source="linkedin",
    )
    assert posting.id == id1
    assert posting.is_remote is True


def test_proxy_manager_sticky_sessions_and_rotation(tmp_path: Path):
    """Verify ProxyManager preserves sticky sessions and rotates upon failure."""
    proxy_mgr = ProxyManager()
    proxy_mgr.add_proxy("http://res1.proxy.com:8000")
    proxy_mgr.add_proxy("http://res2.proxy.com:8000")

    # Sticky session should return same proxy for LinkedIn
    p1 = proxy_mgr.get_proxy_for_board("linkedin")
    p2 = proxy_mgr.get_proxy_for_board("linkedin")
    assert p1 == p2
    assert p1 in ["http://res1.proxy.com:8000", "http://res2.proxy.com:8000"]

    # Rotating proxy on error should select the alternate proxy
    p_rotated = proxy_mgr.mark_proxy_failed("linkedin", p1)
    assert p_rotated != p1
    assert p_rotated in ["http://res1.proxy.com:8000", "http://res2.proxy.com:8000"]


def test_delta_store_deduplication(tmp_path: Path):
    """Verify DeltaStore filters out seen jobs across sweeps."""
    db_file = tmp_path / "test_delta.db"
    store = DeltaStore(db_path=db_file)

    job1 = JobPosting(
        id="job_001",
        title="Backend Engineer",
        company="AlphaCorp",
        job_url="https://alphacorp.com/1",
        description="Python backend",
        source="indeed",
    )
    job2 = JobPosting(
        id="job_002",
        title="ML Engineer",
        company="BetaAI",
        job_url="https://betaai.com/2",
        description="ML model serving",
        source="linkedin",
    )

    # Initial state: neither is seen
    assert store.is_seen("job_001") is False
    assert len(store.filter_unseen([job1, job2])) == 2

    # Mark job1 seen
    store.mark_seen(job1)
    assert store.is_seen("job_001") is True
    assert store.is_seen("job_002") is False

    # Filter batch: only job2 should remain
    unseen = store.filter_unseen([job1, job2])
    assert len(unseen) == 1
    assert unseen[0].id == "job_002"

    # Status update
    store.update_status("job_001", "evaluated")
    assert store.get_seen_count() == 1


def test_scraper_dataframe_normalization():
    """Verify OmnichannelScraper converts pandas DataFrame into JobPosting models."""
    scraper = OmnichannelScraper()

    sample_df = pd.DataFrame([
        {
            "id": "li_99",
            "title": "Principal Systems Engineer",
            "company": "FastCloud",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/99",
            "description": "Architect high-speed pipelines.",
            "site": "linkedin",
            "is_remote": True,
            "min_amount": 180000,
            "max_amount": 220000,
            "job_type": "fulltime",
        }
    ])

    normalized = scraper._normalize_jobspy_df(sample_df, default_source="linkedin")
    assert len(normalized) == 1
    post = normalized[0]
    assert post.title == "Principal Systems Engineer"
    assert post.company == "FastCloud"
    assert post.salary_min == 180000.0
    assert post.salary_max == 220000.0
    assert post.is_remote is True
    assert post.source == "linkedin"



# ==============================================================================
# FILTERING, DEDUPLICATION, AND ATS RELEVANCE
# ==============================================================================

def _posting(**overrides) -> JobPosting:
    """Build a JobPosting with sensible defaults for filter tests."""
    data = dict(
        id="filter01",
        title="Backend Engineer",
        company="Acme",
        job_url="https://acme.com/jobs/1",
        description="Build backend services.",
        location="Remote",
        is_remote=True,
        source="indeed",
    )
    data.update(overrides)
    return JobPosting(**data)


def test_scraper_filters_non_remote_when_remote_only(tmp_path: Path):
    """`is_remote: true` in searches.yaml must actually exclude onsite roles."""
    params = SearchParameters(is_remote=True, target_domains=["Backend Engineer"])
    scraper = OmnichannelScraper(
        search_params=params, delta_store=DeltaStore(db_path=tmp_path / "d.db")
    )

    assert scraper._passes_filters(_posting()) is True
    onsite = _posting(id="filter02", job_url="https://acme.com/jobs/2", location="Austin, TX", is_remote=False)
    assert scraper._passes_filters(onsite) is False
    assert scraper.filter_stats["not_remote"] == 1


def test_scraper_applies_minimum_salary_against_the_top_of_the_band(tmp_path: Path):
    """A 120k-200k band satisfies a 175k floor; its lower bound alone does not."""
    params = SearchParameters(min_salary=175_000, is_remote=False, target_domains=["Backend Engineer"])
    scraper = OmnichannelScraper(
        search_params=params, delta_store=DeltaStore(db_path=tmp_path / "d.db")
    )

    assert scraper._passes_filters(_posting(salary_min=120_000, salary_max=200_000)) is True
    assert scraper._passes_filters(_posting(id="f3", job_url="https://a.com/3", salary_min=90_000, salary_max=110_000)) is False
    # A posting with no salary listed is kept; most postings omit one.
    assert scraper._passes_filters(_posting(id="f4", job_url="https://a.com/4")) is True


def test_scraper_deduplicates_within_a_sweep_keeping_the_richer_copy():
    """The same role found on two boards must be evaluated once, with the fuller text."""
    thin = _posting(description="Short.")
    rich = _posting(description="A far more complete description of the same role.")
    deduped, duplicates = OmnichannelScraper._deduplicate([thin, rich])

    assert duplicates == 1
    assert len(deduped) == 1
    assert deduped[0].description.startswith("A far more complete")


def test_ats_domain_matching_is_token_based_not_substring():
    """The whole-phrase test this replaced matched almost nothing."""
    ats = ATSDirectIngestion()
    domains = ["Senior Distributed Systems Engineer"]

    relevant = _posting(title="Distributed Systems Engineer II", source="greenhouse")
    assert ats.matches_domain(relevant, domains) is True

    unrelated = _posting(id="ats2", job_url="https://a.com/2", title="Graphic Designer", description="Photoshop.", source="greenhouse")
    assert ats.matches_domain(unrelated, domains) is False


def test_ats_matching_requires_two_tokens_so_one_generic_word_cannot_match():
    """'Product Designer' must not match 'Account Executive, Product'."""
    ats = ATSDirectIngestion()
    near_miss = _posting(title="Account Executive, Product - Link", description="Sales role.", source="greenhouse")
    assert ats.matches_domain(near_miss, ["Product Designer"]) is False
    assert ats.matches_domain(_posting(id="ats4", job_url="https://a.com/4", title="Product Designer, Dashboard", source="greenhouse"), ["Product Designer"]) is True


def test_delta_store_rejects_an_unknown_status(tmp_path: Path):
    """A typo'd status would make the job invisible to every status query."""
    store = DeltaStore(db_path=tmp_path / "d.db")
    job = _posting()
    store.mark_seen(job)

    with pytest.raises(ValueError, match="Unknown delta store status"):
        store.update_status(job.id, "definitely_not_a_status")

    store.update_status(job.id, "qualified")
    assert store.status_counts() == {"qualified": 1}


def test_delta_store_reset_clears_everything(tmp_path: Path):
    store = DeltaStore(db_path=tmp_path / "d.db")
    store.mark_many_seen([_posting(), _posting(id="r2", job_url="https://a.com/r2")])
    assert store.get_seen_count() == 2

    store.reset()
    assert store.get_seen_count() == 0


def test_delta_store_reports_outreach_counts(tmp_path: Path):
    store = DeltaStore(db_path=tmp_path / "d.db")
    first = _posting()
    second = _posting(id="mail2", job_url="https://a.com/mail2", title="ML Engineer")

    assert store.outreach_counts() == {"drafts": 0, "recipients": 0, "roles": 0}

    store.record_outreach("HR@Acme.com", first, "Subject one", "Body one")
    store.record_outreach("hr@acme.com", first, "Duplicate", "Duplicate")
    store.record_outreach("careers@acme.com", second, "Subject two", "Body two")

    assert store.outreach_counts() == {"drafts": 2, "recipients": 2, "roles": 2}
