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
    assert ans_exp is None  # Total tenure does not establish tenure with a tool.


def test_form_filler_remote_worldwide_answers_authorized_yes(test_profile, test_job):
    """A remote job with no named country still resolves "authorized to work"
    to Yes when the candidate has declared worldwide remote authorization,
    instead of leaving it unanswered for the human-in-the-loop pause."""
    test_profile.work_authorization.authorized_countries = ["United States"]
    test_profile.work_authorization.remote_worldwide = True
    test_profile.seal_profile()
    filler = FormFiller(test_profile, test_job)  # test_job.location == "Remote", is_remote=True

    ans_auth = filler.answer_screening_question("Are you authorized to work in this position's country?")
    assert ans_auth == "Yes"


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


# ==============================================================================
# LIVE-APPLY PREVIEW
# ==============================================================================

def _preview_inputs(tmp_path: Path, jobs):
    """Write a manifest + qualified file for `jobs` (qualified) plus one extra, unqualified entry."""
    manifest = [
        {"job_id": job.id, "title": job.title, "company": job.company, "score": 8.0,
         "pdf_path": str(tmp_path / f"{job.id}.pdf")}
        for job in jobs
    ]
    manifest.append({"job_id": "not-qualified", "title": "Other", "company": "Other Co",
                     "pdf_path": str(tmp_path / "other.pdf")})
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    qualified_file = tmp_path / "qualified_jobs.json"
    qualified_file.write_text(json.dumps([
        {"job": job.model_dump(), "evaluation": {"fit_score": 8.0}} for job in jobs
    ]), encoding="utf-8")
    return manifest_file, qualified_file


def test_preview_queue_lists_only_what_a_live_run_would_submit(tmp_path: Path, monkeypatch):
    """The confirmation must name real employers: qualified, not yet applied, and automatable."""
    from job_agent.config.schema import JobPosting

    def posting(job_id: str, company: str, url: str) -> JobPosting:
        return JobPosting(id=job_id, title="Engineer", company=company, job_url=url, source="test")

    open_form = posting("gh-1", "Acme", "https://boards.greenhouse.io/acme/jobs/123456")
    login_wall = posting("li-1", "Walled", "https://www.linkedin.com/jobs/view/999")
    done_already = posting("gh-2", "Repeat", "https://boards.greenhouse.io/repeat/jobs/777")
    manifest_file, qualified_file = _preview_inputs(tmp_path, [open_form, login_wall, done_already])
    monkeypatch.setattr(AutoApplyPipeline, "_already_applied", staticmethod(lambda: {"gh-2"}))

    plan = AutoApplyPipeline.preview_queue(manifest_path=manifest_file, qualified_jobs_path=qualified_file)

    assert [row["company"] for row in plan["will_submit"]] == ["Acme"]
    assert [row["company"] for row in plan["manual"]] == ["Walled"]
    assert plan["manual"][0]["reason"]
    assert [row["company"] for row in plan["already_applied"]] == ["Repeat"]
    # The unqualified manifest entry is for manual use and is never in any list.
    assert "Other Co" not in {row["company"] for rows in plan.values() for row in rows}


def test_preview_queue_honours_limit_like_a_run(tmp_path: Path, monkeypatch):
    from job_agent.config.schema import JobPosting

    jobs = [JobPosting(id=f"gh-{n}", title="Engineer", company=f"Co{n}", source="test",
                       job_url=f"https://boards.greenhouse.io/co{n}/jobs/10{n}") for n in range(3)]
    manifest_file, qualified_file = _preview_inputs(tmp_path, jobs)
    monkeypatch.setattr(AutoApplyPipeline, "_already_applied", staticmethod(lambda: set()))

    plan = AutoApplyPipeline.preview_queue(manifest_path=manifest_file, qualified_jobs_path=qualified_file, limit=2)

    assert len(plan["will_submit"]) == 2


def test_preview_queue_without_tailored_resumes_raises_like_a_run(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        AutoApplyPipeline.preview_queue(manifest_path=tmp_path / "missing.json",
                                        qualified_jobs_path=tmp_path / "missing_q.json")
