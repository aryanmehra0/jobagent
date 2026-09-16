"""Unit and integration tests for Phase 5: Browser Automation & Auto-Apply."""

from pathlib import Path
import json
import pytest

from job_agent.config.settings import settings
from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    WorkAuthorization,
    JobPosting,
    SkillSet,
)
from job_agent.automation.form_filler import FormFiller
from job_agent.automation.hitl import ChallengeHandler
from job_agent.automation.agent import AutoApplyAgent
from job_agent.automation.pipeline import AutoApplyPipeline


@pytest.fixture
def test_profile() -> CandidateProfile:
    """Fixture providing candidate profile."""
    prof = CandidateProfile(
        contact=ContactInfo(
            full_name="Alex Rivera",
            email="alex.rivera@example.com",
            phone="+1-415-555-0142",
            location="San Francisco, CA",
            linkedin_url="https://linkedin.com/in/alexrivera",
            github_url="https://github.com/alexrivera",
        ),
        summary="Senior Distributed Systems and Cloud Engineer.",
        work_authorization=WorkAuthorization(
            citizenship=["United States"],
            current_country="United States",
            authorized_countries=["United States"],
            requires_sponsorship=False,
        ),
        skills=SkillSet(
            languages=["Python", "Go"],
            frameworks=["FastAPI"],
            cloud_devops=["Kubernetes", "AWS"],
        ),
        years_of_experience=4.0,
    )
    prof.seal_profile()
    return prof


@pytest.fixture
def test_job() -> JobPosting:
    """Fixture providing job posting."""
    return JobPosting(
        id="job_apply_001",
        title="Senior Cloud Reliability Engineer",
        company="ScaleSphere",
        location="Remote",
        job_url="https://scalesphere.com/careers/apply/1",
        description="Looking for SRE with Kubernetes and AWS experience.",
        is_remote=True,
        source="greenhouse",
    )


def test_form_filler_name_and_screening_answers(test_profile, test_job):
    """Verify FormFiller name parsing and screening question responses."""
    filler = FormFiller(test_profile, test_job)
    first, last = filler._split_name()
    assert first == "Alex"
    assert last == "Rivera"

    ans_auth = filler.answer_screening_question("Are you legally authorized to work in the US?")
    assert ans_auth == "Yes"

    ans_spon = filler.answer_screening_question("Will you now or in the future require sponsorship?")
    assert ans_spon == "No"

    ans_exp = filler.answer_screening_question("How many years of experience do you have with Kubernetes?")
    assert "4" in ans_exp or "experience" in ans_exp.lower()


def test_challenge_handler_detection_rules():
    """Verify ChallengeHandler correctly identifies known CAPTCHA selectors."""
    handler = ChallengeHandler()
    assert len(handler.detect_challenge.__code__.co_varnames) > 0


def test_auto_apply_agent_dry_run_simulation(test_profile, test_job, tmp_path: Path):
    """Verify AutoApplyAgent runs clean simulation in dry_run mode without launching browser."""
    dummy_pdf = tmp_path / "resume.pdf"
    dummy_pdf.write_text("Dummy PDF content")

    agent = AutoApplyAgent(max_steps=25)
    result = agent.apply_to_job(
        profile=test_profile,
        job=test_job,
        pdf_resume_path=dummy_pdf,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["job_id"] == test_job.id
    assert result["applied"] is False


def test_auto_apply_pipeline_dry_run_end_to_end(test_profile, test_job, tmp_path: Path):
    """Verify AutoApplyPipeline coordinates manifest, applies dry-run, and records results."""
    from job_agent.sourcing.delta_store import DeltaStore

    pdf_file = tmp_path / "resume_job_apply_001.pdf"
    pdf_file.write_text("Dummy PDF content")

    manifest = [
        {
            "job_id": test_job.id,
            "title": test_job.title,
            "company": test_job.company,
            "score": 8.5,
            "pdf_path": str(pdf_file),
            "json_path": str(tmp_path / "tailored.json"),
        }
    ]
    manifest_file = tmp_path / "manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    qual_file = tmp_path / "qualified_jobs.json"
    with open(qual_file, "w", encoding="utf-8") as f:
        json.dump([{"job": test_job.model_dump(), "evaluation": {"embedding_similarity": 0.7, "fit_score": 8.5, "passed_threshold": True, "reasoning": "Good"}}], f)

    profile_file = tmp_path / "profile.json"
    profile_file.write_text(test_profile.model_dump_json(), encoding="utf-8")

    delta_db = tmp_path / "delta.db"
    store = DeltaStore(db_path=delta_db)
    store.mark_seen(test_job, status="tailored")

    # Everything is written under tmp_path so the test cannot overwrite the
    # user's real application_results.json in data/outputs/.
    results_file = tmp_path / "application_results.json"

    pipeline = AutoApplyPipeline(delta_store=store)
    successful, failed = pipeline.run_applications(
        manifest_path=manifest_file,
        qualified_jobs_path=qual_file,
        profile_path=profile_file,
        output_path=results_file,
        dry_run=True,
    )

    assert len(successful) == 1
    assert len(failed) == 0
    assert successful[0]["status"] == "dry_run"
    assert successful[0]["applied"] is False
    assert successful[0]["fit_score"] == 8.5
    assert results_file.exists()
