"""Tests for the anti-hallucination guarantees.

The agent writes resumes and emails that go to real employers, so the property
these tests protect is the most important one in the codebase: **no stage may
assert a fact about the candidate that is not in their resume.**

Two gates enforce it:

* Intake (`audit_metrics_against_source`) rejects extracted metrics absent from
  the resume text.
* Tailoring (`enforce_metric_integrity`) rejects rewritten bullets carrying
  metrics absent from the sealed profile, and restores ones the rewrite dropped.
"""

from pathlib import Path

import pytest

from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    JobPosting,
    LockedFact,
    SkillSet,
    WorkAuthorization,
    WorkExperience,
)
from job_agent.intake.heuristic import ResumeParseError, build_profile_dict
from job_agent.intake.validator import audit_metrics_against_source, validate_and_save_profile
from job_agent.tailoring.rewriter import ResumeTailorer
from job_agent.tracking.cold_email import ColdEmailGenerator

RESUME_TEXT = """Alex Rivera
San Francisco, CA | alex.rivera@example.com | +1 (415) 555-0142 | linkedin.com/in/alexrivera

PROFESSIONAL SUMMARY
Senior infrastructure engineer building resilient microservices.

WORK EXPERIENCE
ScaleFlow Technologies - Lead Infrastructure Engineer (2022 - Present)
- Architected multi-region Kubernetes clusters supporting over 200k RPS.
- Reduced AWS infrastructure costs by $340k annually.

CloudPulse Inc - Senior Software Engineer (2020 - 2022)
- Designed an event ingestion pipeline processing 50M events daily.

EDUCATION
Stanford University - M.S. in Computer Science (2018 - 2020)

TECHNICAL SKILLS
Languages: Python, Go, Rust
Cloud & DevOps: AWS, Kubernetes, Terraform
"""


@pytest.fixture
def sealed_profile(tmp_path: Path) -> CandidateProfile:
    """A profile extracted from RESUME_TEXT and sealed."""
    data = build_profile_dict(RESUME_TEXT, source_document="test_resume.pdf")
    return validate_and_save_profile(data, tmp_path / "profile.json", source_text=RESUME_TEXT)


# ==============================================================================
# INTAKE GATE
# ==============================================================================

def test_deterministic_parser_extracts_only_real_facts():
    """The offline parser must reproduce the resume, not a plausible-looking one."""
    data = build_profile_dict(RESUME_TEXT)

    assert data["contact"]["full_name"] == "Alex Rivera"
    assert data["contact"]["email"] == "alex.rivera@example.com"
    assert [exp["company"] for exp in data["experience"]] == ["ScaleFlow Technologies", "CloudPulse Inc"]
    assert data["education"][0]["institution"] == "Stanford University"

    # Nothing the resume does not mention may appear.
    blob = str(data).lower()
    for invented in ("tech corp", "university of technology", "aws certified", "candidate@example.com"):
        assert invented not in blob, f"parser invented {invented!r}"

    # A resume with no certifications section must yield no certifications.
    assert data["certifications"] == []


def test_parser_raises_rather_than_inventing_identity():
    """Name and email cannot be derived from anything, so absence must be an error."""
    with pytest.raises(ResumeParseError) as exc_info:
        build_profile_dict("Some text with no name and no contact details whatsoever.")
    assert "email" in str(exc_info.value)


def test_years_of_experience_is_computed_from_dates():
    data = build_profile_dict(RESUME_TEXT)
    # 2020 to today, with the two roles adjacent rather than overlapping.
    assert data["years_of_experience"] >= 4.0


def test_intake_audit_rejects_metrics_absent_from_the_resume():
    """An LLM-invented metric must be stripped before it is sealed as truth."""
    extracted = {
        "experience": [
            {
                "company": "ScaleFlow Technologies",
                "locked_facts": [
                    {"category": "scale", "statement": "Handled 200k RPS.", "metric_value": "200k RPS"},
                    {"category": "revenue", "statement": "Saved $9.9M.", "metric_value": "$9.9M"},
                    {
                        "category": "metric",
                        "statement": "Cut costs by $340k and improved uptime 99.99%.",
                        "metric_value": "$340k, 99.99%",
                    },
                ],
            }
        ],
        "projects": [],
    }

    audited, rejected = audit_metrics_against_source(extracted, RESUME_TEXT)
    facts = audited["experience"][0]["locked_facts"]

    # The wholly invented fact is gone; the partly invented one keeps only its real metric.
    statements = [fact["statement"] for fact in facts]
    assert "Saved $9.9M." not in statements
    assert "$9.9M" in rejected and "99.99%" in rejected

    mixed = next(fact for fact in facts if fact["statement"].startswith("Cut costs"))
    assert mixed["metric_value"] == "$340k"


def test_intake_audit_is_insensitive_to_metric_formatting():
    """'$340,000' in the resume must verify a '$340,000' extraction regardless of spacing."""
    data = {"experience": [{"locked_facts": [{"statement": "Saved money.", "metric_value": "$ 340,000"}]}], "projects": []}
    audited, rejected = audit_metrics_against_source(data, "Reduced spend by $340,000 last year.")
    assert rejected == []
    assert audited["experience"][0]["locked_facts"]


# ==============================================================================
# TAILORING GATE
# ==============================================================================

@pytest.fixture
def duplicate_company_profile() -> CandidateProfile:
    """A candidate promoted within one employer, which is the case that used to merge."""
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Alex Rivera", email="alex@example.com", location="SF, CA"),
        summary="Infrastructure engineer specializing in distributed systems.",
        work_authorization=WorkAuthorization(current_country="United States"),
        experience=[
            WorkExperience(
                company="ScaleFlow",
                title="Lead Engineer",
                start_date="2022-01",
                description_bullets=["Architected clusters supporting 200k RPS.", "Reduced AWS costs by $340k annually."],
                locked_facts=[
                    LockedFact(category="scale", statement="Architected clusters supporting 200k RPS.", metric_value="200k RPS"),
                    LockedFact(category="revenue", statement="Reduced AWS costs by $340k annually.", metric_value="$340k"),
                ],
            ),
            WorkExperience(
                company="ScaleFlow",
                title="Senior Engineer",
                start_date="2020-01",
                end_date="2021-12",
                description_bullets=["Built an ingestion pipeline handling 50M events daily."],
                locked_facts=[
                    LockedFact(category="scale", statement="Built an ingestion pipeline handling 50M events daily.", metric_value="50M events")
                ],
            ),
        ],
        skills=SkillSet(languages=["Python"], cloud_devops=["AWS", "Kubernetes"]),
        years_of_experience=5.0,
    )
    return profile.seal_profile()


def test_tailoring_restores_dropped_locked_metrics(duplicate_company_profile):
    """A rewrite that loses an achievement must have it put back."""
    stripped = {
        "tailored_summary": "Cloud engineer.",
        "tailored_experience": [
            {
                "company": "ScaleFlow",
                "title": "Lead Engineer",
                "start_date": "2022-01",
                "tailored_bullets": ["Managed Kubernetes clusters.", "Reduced cloud spend using spot instances."],
            }
        ],
    }
    _, restored, fabricated = ResumeTailorer().enforce_metric_integrity(duplicate_company_profile, stripped)
    assert "200k RPS" in restored and "$340k" in restored
    assert fabricated == []


def test_tailoring_blocks_invented_metrics(duplicate_company_profile):
    """A rewrite that invents a number must not reach the PDF."""
    inflated = {
        "tailored_summary": "Cloud engineer.",
        "tailored_experience": [
            {
                "company": "ScaleFlow",
                "title": "Lead Engineer",
                "start_date": "2022-01",
                "tailored_bullets": [
                    "Architected clusters supporting 200k RPS.",
                    "Reduced AWS costs by 72% and improved uptime to 99.99%.",
                    "Won an industry award worth $5M.",
                ],
            }
        ],
    }
    data, _, fabricated = ResumeTailorer().enforce_metric_integrity(duplicate_company_profile, inflated)

    assert {"72%", "99.99%", "$5M"} <= set(fabricated)
    all_text = " ".join(
        bullet for entry in data["tailored_experience"] for bullet in entry["tailored_bullets"]
    )
    for invented in ("72%", "99.99%", "$5M"):
        assert invented not in all_text
    # The real figures survive.
    assert "200k RPS" in all_text and "$340k" in all_text


def test_tailoring_keeps_roles_at_the_same_company_separate(duplicate_company_profile):
    """Two roles at one employer must not have their bullets merged."""
    tailored = {
        "tailored_summary": "Engineer.",
        "tailored_experience": [
            {"company": "ScaleFlow", "title": "Lead Engineer", "start_date": "2022-01", "tailored_bullets": ["Architected clusters supporting 200k RPS."]},
            {"company": "ScaleFlow", "title": "Senior Engineer", "start_date": "2020-01", "tailored_bullets": ["Built an ingestion pipeline handling 50M events daily."]},
        ],
    }
    data, _, _ = ResumeTailorer().enforce_metric_integrity(duplicate_company_profile, tailored)

    assert len(data["tailored_experience"]) == 2
    lead = next(e for e in data["tailored_experience"] if e["title"] == "Lead Engineer")
    senior = next(e for e in data["tailored_experience"] if e["title"] == "Senior Engineer")
    assert "50M events" not in " ".join(lead["tailored_bullets"])
    assert "200k RPS" not in " ".join(senior["tailored_bullets"])


def test_tailoring_never_mutates_the_sealed_profile(duplicate_company_profile):
    """The integrity gate must not become a way of editing the truth source."""
    original_hash = duplicate_company_profile.fact_hash
    job = JobPosting(id="job12345", title="Cloud Engineer", company="TestCo", job_url="https://t.co/1", description="Kubernetes AWS", source="greenhouse")
    ResumeTailorer().generate_tailored_profile_data(duplicate_company_profile, job)
    assert duplicate_company_profile.fact_hash == original_hash
    assert duplicate_company_profile.verify_integrity() is True


def test_tailored_payload_carries_certifications_and_an_end_date(sealed_profile):
    """Both were previously dropped, so the template could not render them."""
    job = JobPosting(id="job12346", title="Cloud Engineer", company="TestCo", job_url="https://t.co/2", description="AWS", source="greenhouse")
    payload = ResumeTailorer().generate_tailored_profile_data(sealed_profile, job)

    assert "certifications" in payload
    for role in payload["experience"]:
        assert role["end_date"], "end_date must never be null; Typst prints it directly"


# ==============================================================================
# OUTREACH GATE
# ==============================================================================

def test_cold_email_cites_only_profile_facts(sealed_profile):
    """The offline outreach template must not add claims of its own."""
    job = JobPosting(id="job12347", title="Staff Engineer", company="Stripe", job_url="https://stripe.com/j/1", description="x", source="greenhouse")
    email = ColdEmailGenerator()._heuristic_email(sealed_profile, job)

    assert sealed_profile.contact.full_name in email
    assert "Stripe" in email
    for unfounded in ("impressed by your work", "high-availability environments"):
        assert unfounded not in email


def test_cold_email_omits_missing_contact_details():
    """A profile with no phone or LinkedIn must not get an invented one."""
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Jane Doe", email="jane@example.com"),
        summary="Backend engineer working on payments infrastructure.",
        work_authorization=WorkAuthorization(current_country="United States"),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=2.0,
    ).seal_profile()

    job = JobPosting(id="job12348", title="Engineer", company="Acme", job_url="https://acme.com/1", description="x", source="lever")
    email = ColdEmailGenerator()._heuristic_email(profile, job)

    assert "linkedin.com" not in email
    assert "555" not in email
    assert "jane@example.com" in email
