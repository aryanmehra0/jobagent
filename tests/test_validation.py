"""Schema validation tests.

These cover the guarantees the rest of the pipeline relies on: that bad input is
rejected at the boundary, that values are normalized consistently, and that
derived fields cannot be set to something inconsistent with what they derive from.
"""

import pytest
from pydantic import ValidationError

from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    Education,
    EvaluationScore,
    JobPosting,
    LockedFact,
    RerankerVerdict,
    SearchParameters,
    SkillSet,
    WorkAuthorization,
    WorkExperience,
)


# --- ContactInfo ------------------------------------------------------------

def test_contact_normalizes_urls_and_drops_bad_phone():
    """A bare URL gains a scheme; an implausible phone number is dropped, not kept."""
    contact = ContactInfo(
        full_name="  Alex   Rivera ",
        email="alex@example.com",
        location="San Francisco, CA",
        linkedin_url="linkedin.com/in/alex",
        phone="12345",
    )
    assert contact.full_name == "Alex Rivera"
    assert contact.linkedin_url == "https://linkedin.com/in/alex"
    assert contact.phone is None


def test_contact_rejects_invalid_email():
    with pytest.raises(ValidationError):
        ContactInfo(full_name="Alex Rivera", email="not-an-email", location="Remote")


def test_contact_location_is_optional():
    """A resume without a location must not force a fabricated one."""
    assert ContactInfo(full_name="Alex Rivera", email="a@b.com").location is None


# --- WorkAuthorization ------------------------------------------------------

def test_work_authorization_defaults_to_country_of_residence():
    auth = WorkAuthorization(current_country="United States")
    assert auth.authorized_countries == ["United States"]


def test_is_authorized_in_returns_none_when_unknown():
    """An unknown location must be reported as unknown, never guessed."""
    auth = WorkAuthorization(current_country="United States")
    assert auth.is_authorized_in("Remote - United States") is True
    assert auth.is_authorized_in("San Francisco, USA") is True
    assert auth.is_authorized_in("Berlin, Germany") is None


# --- Dates ------------------------------------------------------------------

def test_experience_rejects_inverted_date_range():
    with pytest.raises(ValidationError, match="precedes start_date"):
        WorkExperience(company="X", title="Engineer", start_date="2022-01", end_date="2020-01")


def test_education_rejects_inverted_date_range():
    with pytest.raises(ValidationError, match="precedes start_date"):
        Education(institution="MIT", degree="B.S.", field_of_study="CS", start_date="2020", end_date="2016")


def test_experience_reconciles_is_current_with_end_date():
    """`is_current` and `end_date` must agree; an explicit past end date wins."""
    ongoing = WorkExperience(company="X", title="Engineer", start_date="2022-01", end_date=None)
    assert ongoing.end_date == "Present" and ongoing.is_current is True

    finished = WorkExperience(
        company="X", title="Engineer", start_date="2020-01", end_date="2022-01", is_current=True
    )
    assert finished.is_current is False


def test_experience_key_distinguishes_roles_at_the_same_company():
    """Keying tailored bullets by company alone would merge a promotion."""
    junior = WorkExperience(company="ScaleFlow", title="Engineer", start_date="2020-01", end_date="2021-12")
    senior = WorkExperience(company="ScaleFlow", title="Lead Engineer", start_date="2022-01")
    assert junior.key != senior.key


# --- Bullets and skills -----------------------------------------------------

def test_bullets_are_stripped_deduplicated_and_cleaned():
    exp = WorkExperience(
        company="X",
        title="Engineer",
        start_date="2020",
        description_bullets=["• Built the thing", "Built the thing", "  ", "x"],
    )
    assert exp.description_bullets == ["Built the thing"]


def test_skills_accept_a_comma_joined_string():
    """LLMs sometimes return a string where the schema declares a list."""
    skills = SkillSet(languages="Python, Go; Rust")
    assert skills.languages == ["Python", "Go", "Rust"]


def test_all_skills_dedupes_across_categories():
    skills = SkillSet(languages=["Python"], frameworks=["python"], cloud_devops=["AWS"])
    assert skills.all_skills() == ["Python", "AWS"]


# --- SearchParameters -------------------------------------------------------

def test_search_parameters_reject_unknown_board():
    """A typo must fail loudly rather than silently returning zero jobs."""
    with pytest.raises(ValidationError, match="Unsupported job board"):
        SearchParameters(job_boards=["linkedin", "monster"])


def test_search_parameters_accept_ziprecruiter_spelling_variant():
    assert "zip_recruiter" in SearchParameters(job_boards=["ZipRecruiter"]).job_boards


@pytest.mark.parametrize("field,value", [("hours_old", 0), ("hours_old", 99999), ("min_salary", -5), ("max_results_per_board", 0)])
def test_search_parameters_enforce_ranges(field, value):
    with pytest.raises(ValidationError):
        SearchParameters(**{field: value})


def test_search_parameters_reject_unsupported_ats_provider():
    with pytest.raises(ValidationError, match="Unsupported ATS provider"):
        SearchParameters(ats_companies={"workday": ["acme"]})


def test_search_parameters_normalize_proxy_url():
    assert SearchParameters(proxy_url="user:pass@gate.proxy.com:8000").proxy_url.startswith("http://")
    with pytest.raises(ValidationError, match="Invalid proxy URL"):
        SearchParameters(proxy_url="not a proxy at all")


# --- JobPosting -------------------------------------------------------------

def test_job_posting_requires_a_usable_url():
    """A posting with no application URL cannot be applied to."""
    with pytest.raises(ValidationError, match="valid http"):
        JobPosting(id="abc123", title="Engineer", company="X", job_url="", source="indeed")


def test_job_posting_id_ignores_tracking_query_parameters():
    """The same posting served with session tracking must deduplicate to one job."""
    plain = JobPosting.create_id("https://boards.greenhouse.io/x/jobs/1", "X", "Engineer")
    tracked = JobPosting.create_id("https://boards.greenhouse.io/x/jobs/1?gh_src=abc123", "X", "Engineer")
    assert plain == tracked


def test_job_posting_infers_remote_from_location():
    job = JobPosting(id="abc123", title="Engineer", company="X", job_url="https://x.com/1", location="Remote - US", source="indeed")
    assert job.is_remote is True


def test_job_posting_rejects_negative_salary():
    with pytest.raises(ValidationError):
        JobPosting(id="abc123", title="E", company="X", job_url="https://x.com/1", source="indeed", salary_min=-1)


# --- Scoring ----------------------------------------------------------------

def test_reranker_verdict_rescales_out_of_range_scores():
    """A model answering on a 0-100 scale is rescaled rather than discarded."""
    assert RerankerVerdict(fit_score=85, reasoning="ok").fit_score == 8.5
    assert RerankerVerdict(fit_score="8.5/10", reasoning="ok").fit_score == 8.5
    assert RerankerVerdict(fit_score=9999, reasoning="ok").fit_score == 10.0


def test_evaluation_score_derives_passed_threshold_from_the_score():
    """A judge claiming a failing score passed must not reach the apply queue."""
    score = EvaluationScore(
        embedding_similarity=0.5,
        fit_score=4.0,
        threshold_used=7.0,
        passed_threshold=True,  # The model's claim, which must be overridden.
        reasoning="weak match",
    )
    assert score.passed_threshold is False

    strong = EvaluationScore(
        embedding_similarity=0.5, fit_score=8.0, threshold_used=7.0, passed_threshold=False, reasoning="strong"
    )
    assert strong.passed_threshold is True


def test_evaluation_score_clamps_similarity():
    assert EvaluationScore(embedding_similarity=1.9, fit_score=7.0, reasoning="x").embedding_similarity == 1.0


# --- CandidateProfile -------------------------------------------------------

def _profile(**overrides) -> CandidateProfile:
    """Build a minimal valid profile for the tests below."""
    data = dict(
        contact=ContactInfo(full_name="Alex Rivera", email="alex@example.com", location="SF, CA"),
        summary="Infrastructure engineer specializing in distributed systems.",
        work_authorization=WorkAuthorization(current_country="United States"),
        experience=[
            WorkExperience(
                company="ScaleFlow",
                title="Lead Engineer",
                start_date="2020-01",
                end_date="2024-01",
                description_bullets=["Scaled the platform to 200k RPS."],
                locked_facts=[LockedFact(category="scale", statement="Scaled the platform to 200k RPS.", metric_value="200k RPS")],
            )
        ],
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    )
    data.update(overrides)
    return CandidateProfile(**data)


def test_profile_seal_detects_tampering():
    profile = _profile()
    profile.seal_profile()
    assert profile.verify_integrity() is True

    profile.experience[0].locked_facts[0].metric_value = "900k RPS"
    assert profile.verify_integrity() is False


def test_profile_rejects_malformed_fact_hash():
    with pytest.raises(ValidationError, match="SHA-256"):
        _profile(fact_hash="not-a-hash")


def test_profile_computes_experience_from_role_dates():
    profile = _profile()
    assert profile.computed_years_of_experience() == 4.0
    assert profile.experience_discrepancy() == 0.0


def test_profile_reports_experience_discrepancy():
    """A stated total that contradicts the dates is surfaced, not silently trusted."""
    profile = _profile(years_of_experience=15.0)
    assert profile.experience_discrepancy() == pytest.approx(11.0, abs=0.2)


def test_profile_rejects_impossible_experience():
    with pytest.raises(ValidationError):
        _profile(years_of_experience=120.0)


def test_strict_models_reject_unknown_fields():
    """A renamed LLM key must fail loudly instead of being dropped."""
    with pytest.raises(ValidationError):
        ContactInfo(full_name="Alex Rivera", email="a@b.com", nickname="Al")
