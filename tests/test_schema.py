"""Unit tests for CandidateProfile and SearchParameters schema models."""

import pytest
from pydantic import ValidationError

from job_agent.config.schema import (
    ContactInfo,
    WorkAuthorization,
    Education,
    WorkExperience,
    SkillSet,
    Project,
    LockedFact,
    CandidateProfile,
    SearchParameters,
)
from job_agent.intake.validator import extract_potential_metrics, enrich_locked_facts


def create_sample_profile() -> CandidateProfile:
    """Helper to construct a valid CandidateProfile."""
    return CandidateProfile(
        contact=ContactInfo(
            full_name="Jane Doe",
            email="jane.doe@example.com",
            phone="+1-555-0100",
            location="San Francisco, CA",
            linkedin_url="https://linkedin.com/in/janedoe",
            github_url="https://github.com/janedoe",
        ),
        summary="Senior Backend Engineer specializing in high-throughput microservices.",
        work_authorization=WorkAuthorization(
            citizenship=["United States"],
            current_country="United States",
            authorized_countries=["United States"],
            requires_sponsorship=False,
        ),
        education=[
            Education(
                institution="UC Berkeley",
                degree="B.S.",
                field_of_study="EECS",
                start_date="2016",
                end_date="2020",
                gpa="3.9",
            )
        ],
        experience=[
            WorkExperience(
                company="CloudScale Inc",
                title="Senior Software Engineer",
                location="San Francisco, CA",
                start_date="2020-08",
                end_date="Present",
                is_current=True,
                description_bullets=[
                    "Scaled API infrastructure to handle over 150k RPS with 99.99% reliability.",
                    "Reduced AWS infrastructure costs by $120k annually through DynamoDB caching.",
                ],
                locked_facts=[
                    LockedFact(
                        category="scale",
                        statement="Scaled API infrastructure to handle over 150k RPS with 99.99% reliability.",
                        metric_value="150k RPS, 99.99%",
                    ),
                    LockedFact(
                        category="revenue",
                        statement="Reduced AWS infrastructure costs by $120k annually through DynamoDB caching.",
                        metric_value="$120k",
                    ),
                ],
            )
        ],
        skills=SkillSet(
            languages=["Python", "Go", "Rust"],
            frameworks=["FastAPI", "gRPC", "Django"],
            developer_tools=["Docker", "PostgreSQL", "Kafka"],
            cloud_devops=["AWS", "Kubernetes", "Terraform"],
            domain_knowledge=["Microservices", "Event-Driven Architecture"],
        ),
        projects=[
            Project(
                title="Distributed Rate Limiter",
                description="Token bucket rate limiter benchmarked at 250k RPS.",
                technologies=["Go", "Redis"],
                locked_facts=[
                    LockedFact(
                        category="scale",
                        statement="Token bucket rate limiter benchmarked at 250k RPS.",
                        metric_value="250k RPS",
                    )
                ],
            )
        ],
        years_of_experience=4.0,
    )


def test_candidate_profile_creation_and_hash():
    """Verify CandidateProfile seals properly and computes deterministic SHA-256 hash."""
    profile = create_sample_profile()
    profile.seal_profile()

    assert profile.fact_hash is not None
    assert len(profile.fact_hash) == 64  # Valid SHA-256 hex string
    assert profile.verify_integrity() is True


def test_candidate_profile_tamper_detection():
    """Verify that tampering with a locked fact invalidates profile integrity."""
    profile = create_sample_profile()
    profile.seal_profile()
    original_hash = profile.fact_hash

    # Tamper with locked fact metric
    profile.experience[0].locked_facts[0].statement = "Scaled API infrastructure to handle over 900k RPS."
    
    # Hash check must fail
    assert profile.verify_integrity() is False
    assert profile.compute_fact_hash() != original_hash


def test_metric_extraction_regex():
    """Verify regex heuristic correctly captures percentages, currency, multipliers, and scale."""
    sample_text = "Achieved 42% latency reduction, saved $2.5M, sped up pipelines by 4x across 100k requests."
    metrics = extract_potential_metrics(sample_text)

    assert "42%" in metrics
    assert "$2.5M" in metrics
    assert "4x" in metrics
    assert "100k requests" in metrics


def test_enrich_locked_facts():
    """Verify automatic enrichment of locked facts from raw bullet points."""
    raw_dict = {
        "experience": [
            {
                "company": "StartupX",
                "title": "Engineer",
                "start_date": "2021",
                "description_bullets": [
                    "Improved search query response by 35% across 50,000 users.",
                    "General maintenance and documentation updates.",
                ],
                "locked_facts": [],
            }
        ],
        "projects": [],
    }

    enriched = enrich_locked_facts(raw_dict)
    locked = enriched["experience"][0]["locked_facts"]
    assert len(locked) == 1
    assert "35%" in locked[0]["metric_value"]


def test_search_parameters_schema():
    """Verify SearchParameters schema defaults and custom validation."""
    params = SearchParameters(
        target_domains=["AI Engineer", "MLOps Engineer"],
        desired_experience_years=5.0,
        locations=["Remote"],
        hours_old=24,
    )
    assert params.target_domains == ["AI Engineer", "MLOps Engineer"]
    assert params.hours_old == 24
    assert params.is_remote is True
    assert "linkedin" in params.job_boards

