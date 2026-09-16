"""Unit and integration tests for Phase 1 Candidate Intake & Parameterization."""

from pathlib import Path
import pytest
import yaml

from job_agent.config.schema import SearchParameters
from job_agent.intake.parser import ResumeParser
from job_agent.intake.validator import load_and_verify_profile
from job_agent.intake.cli import save_search_parameters, load_search_parameters


@pytest.fixture
def sample_pdf_path() -> Path:
    """Fixture returning path to the sample test PDF."""
    path = Path("data/raw_resumes/sample_resume.pdf")
    if not path.exists():
        from tests.generate_test_resume import generate_sample_pdf
        generate_sample_pdf(path)
    return path


def test_extract_text_from_pdf(sample_pdf_path: Path):
    """Verify that text extraction recovers key terms from the PDF."""
    parser = ResumeParser()
    text = parser.extract_text_from_pdf(sample_pdf_path)

    assert len(text) > 200
    assert "Alex Rivera" in text
    assert "ScaleFlow Technologies" in text
    assert "Kubernetes" in text
    assert "$340k" in text


def test_resume_parser_end_to_end(sample_pdf_path: Path, tmp_path: Path):
    """Verify complete PDF intake -> profile.json creation with cryptographic sealing."""
    test_output = tmp_path / "test_profile.json"
    parser = ResumeParser(provider="offline")

    profile = parser.parse(pdf_path=sample_pdf_path, output_path=test_output)

    assert test_output.exists()
    assert profile.contact.full_name is not None
    assert profile.fact_hash is not None
    assert len(profile.experience) > 0

    # Load and verify from disk
    loaded_profile, is_valid = load_and_verify_profile(test_output)
    assert is_valid is True
    assert loaded_profile.fact_hash == profile.fact_hash


def test_search_parameters_serialization(tmp_path: Path):
    """Verify serialization to YAML and reload of searches.yaml."""
    yaml_path = tmp_path / "searches.yaml"
    params = SearchParameters(
        target_domains=["Backend Engineer", "Platform Engineer"],
        desired_experience_years=5.0,
        locations=["Remote", "Seattle, WA"],
        is_remote=True,
        hours_old=24,
        job_boards=["linkedin", "indeed"],
        min_salary=160000,
    )

    save_search_parameters(params, yaml_path)
    assert yaml_path.exists()

    loaded = load_search_parameters(yaml_path)
    assert loaded.target_domains == ["Backend Engineer", "Platform Engineer"]
    assert loaded.desired_experience_years == 5.0
    assert loaded.min_salary == 160000
    assert loaded.hours_old == 24

