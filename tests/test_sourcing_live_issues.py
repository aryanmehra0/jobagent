"""Regressions for failures seen in a live sourcing sweep over Indian locations.

The sweep searched three roles across seven locations on four boards. It spent
most of its time on boards that had already blocked it, lost whole LinkedIn
result pages to one foreign listing, searched the US Indeed site for Indian
cities, and could not geocode a misspelled city.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.sourcing.scraper import OmnichannelScraper


def _posting(**overrides) -> JobPosting:
    data = dict(id="live0001", title="Associate Product Manager", company="Acme",
                job_url="https://acme.example/jobs/1", description="Product role.",
                location="Remote", is_remote=True, source="indeed")
    data.update(overrides)
    return JobPosting(**data)


# ==============================================================================
# SEARCH PARAMETERS
# ==============================================================================

@pytest.mark.parametrize(
    "typed,expected",
    [
        ("gurgoan", "Gurugram"), ("gurgaon", "Gurugram"), ("Gurugram", "Gurugram"),
        ("banglore", "Bengaluru"), ("bangalore", "Bengaluru"),
        ("bombay", "Mumbai"), ("mumbai", "Mumbai"),
        ("new delhi", "New Delhi"), ("noida", "Noida"), ("hyderabad", "Hyderabad"),
        ("Remote", "Remote"),
    ],
)
def test_common_city_misspellings_are_normalised(typed, expected):
    """"gurgoan" returned no LinkedIn results because the board could not geocode it."""
    assert SearchParameters(locations=[typed]).locations == [expected]


def test_unrecognised_locations_are_kept_as_typed_apart_from_casing():
    """Only known misspellings are corrected; nothing else is guessed at."""
    assert SearchParameters(locations=["springfield, IL"]).locations == ["springfield, IL"]


def test_misspellings_that_normalise_to_the_same_city_are_deduplicated():
    assert SearchParameters(locations=["Bangalore", "banglore", "Bengaluru"]).locations == ["Bengaluru"]


def test_country_indeed_must_be_a_country_jobspy_supports():
    """An unsupported value fails mid-sweep inside JobSpy; reject it at load time."""
    with pytest.raises(ValueError, match="country_indeed"):
        SearchParameters(country_indeed="atlantis")
    assert SearchParameters(country_indeed="India").country_indeed == "india"


def test_salary_currency_defaults_to_usd_and_is_normalised():
    assert SearchParameters().salary_currency == "USD"
    assert SearchParameters(salary_currency="inr").salary_currency == "INR"


def test_indian_locations_with_a_us_indeed_country_are_flagged():
    """Indeed searched indeed.com for Mumbai and returned nothing for every city."""
    params = SearchParameters(locations=["Remote", "mumbai", "gurgoan"], country_indeed="usa")
    warnings = OmnichannelScraper.configuration_warnings(params)
    assert any("country_indeed" in warning and "india" in warning for warning in warnings)


def test_matching_indeed_country_raises_no_warning():
    params = SearchParameters(locations=["Remote", "mumbai"], country_indeed="india", salary_currency="INR")
    assert not any("country_indeed" in w for w in OmnichannelScraper.configuration_warnings(params))


# ==============================================================================
# SALARY FLOOR CURRENCY
# ==============================================================================

def _scraper(tmp_path: Path, **params) -> OmnichannelScraper:
    return OmnichannelScraper(search_params=SearchParameters(**params),
                              delta_store=DeltaStore(db_path=tmp_path / "d.db"))


def test_salary_floor_ignores_postings_in_another_currency(tmp_path: Path):
    """An INR floor of 800,000 must not reject a USD band of 120k-150k."""
    scraper = _scraper(tmp_path, is_remote=False, min_salary=800_000, salary_currency="INR",
                       target_domains=["Associate Product Manager"])
    usd = _posting(salary_min=120_000, salary_max=150_000, salary_currency="USD")
    assert scraper._passes_filters(usd) is True


def test_salary_floor_applies_to_postings_in_the_same_currency(tmp_path: Path):
    scraper = _scraper(tmp_path, is_remote=False, min_salary=800_000, salary_currency="INR",
                       target_domains=["Associate Product Manager"])
    low = _posting(id="live0002", job_url="https://acme.example/jobs/2",
                   salary_min=400_000, salary_max=600_000, salary_currency="INR")
    high = _posting(id="live0003", job_url="https://acme.example/jobs/3",
                    salary_min=900_000, salary_max=1_400_000, salary_currency="INR")
    assert scraper._passes_filters(low) is False
    assert scraper._passes_filters(high) is True


# ==============================================================================
# BLOCKED BOARDS
# ==============================================================================

class _FakeJobSpy:
    """Mimics JobSpy: a blocked board is logged at ERROR and returns no rows."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.calls = []

    def scrape_jobs(self, site_name, **kwargs):
        import pandas as pd

        from job_agent.sourcing.scraper import JOBSPY_LOGGER_NAMES

        board = site_name[0]
        self.calls.append(board)
        if board in self.blocked:
            # Exactly as JobSpy does it: a non-propagating logger per board.
            logger = logging.getLogger(f"JobSpy:{JOBSPY_LOGGER_NAMES[board]}")
            logger.propagate = False
            logger.error(f"{board}: bad response status code: 403")
        return pd.DataFrame()


def test_a_board_that_returns_403_is_skipped_for_the_rest_of_the_sweep(tmp_path: Path):
    """JobSpy logs a block instead of raising, so the sweep retried it 21 times."""
    scraper = _scraper(tmp_path, target_domains=["APM", "AI PM"], locations=["Remote", "Mumbai", "Delhi"],
                       job_boards=["linkedin", "glassdoor", "zip_recruiter"], is_remote=False)
    fake = _FakeJobSpy(blocked={"glassdoor", "zip_recruiter"})

    for domain in scraper.params.target_domains:
        for location in scraper.params.locations:
            for board in scraper.params.job_boards:
                if scraper.is_board_blocked(board):
                    continue
                scraper._scrape_single_board(fake, board=board, domain=domain, location=location, results_wanted=5)

    assert fake.calls.count("glassdoor") == 1
    assert fake.calls.count("zip_recruiter") == 1
    assert fake.calls.count("linkedin") == 6, "a healthy board must keep being queried"


def test_a_board_that_merely_finds_nothing_is_not_blocked(tmp_path: Path):
    scraper = _scraper(tmp_path, is_remote=False)
    fake = _FakeJobSpy()
    scraper._scrape_single_board(fake, board="indeed", domain="APM", location="Noida", results_wanted=5)
    assert scraper.is_board_blocked("indeed") is False


# ==============================================================================
# JOBSPY UNKNOWN COUNTRY
# ==============================================================================

def test_one_listing_in_an_unknown_country_does_not_discard_the_page():
    """JobSpy raises on "Sri Lanka", losing every other result for the query."""
    from jobspy.model import Country

    from job_agent.sourcing.scraper import tolerate_unknown_countries

    with pytest.raises(ValueError):
        Country.from_string("sri lanka")

    with tolerate_unknown_countries():
        assert Country.from_string("sri lanka") == Country.WORLDWIDE
        assert Country.from_string("india") == Country.INDIA

    # The patch is scoped: normal JobSpy behaviour returns afterwards.
    with pytest.raises(ValueError):
        Country.from_string("sri lanka")
