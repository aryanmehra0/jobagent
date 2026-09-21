"""Unit and integration tests for Phase 4: Dynamic Resume Tailoring & Typst Compilation."""

from pathlib import Path
import json
import pytest
import pypdf

from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    WorkAuthorization,
    Education,
    WorkExperience,
    SkillSet,
    Project,
    LockedFact,
    JobPosting,
    EvaluatedJob,
    EvaluationScore,
)
from job_agent.tailoring.rewriter import ResumeTailorer
from job_agent.tailoring.compiler import TypstResumeCompiler
from job_agent.tailoring.pipeline import ResumeTailoringPipeline


@pytest.fixture
def candidate_profile() -> CandidateProfile:
    """Fixture providing candidate profile with locked facts."""
    prof = CandidateProfile(
        contact=ContactInfo(
            full_name="Alex Rivera",
            email="alex.rivera@example.com",
            phone="+1-415-555-0142",
            location="San Francisco, CA",
        ),
        summary="Lead Infrastructure Engineer specializing in Kubernetes and distributed systems.",
        work_authorization=WorkAuthorization(
            citizenship=["United States"],
            current_country="United States",
            authorized_countries=["United States"],
        ),
        education=[
            Education(
                institution="Stanford University",
                degree="M.S.",
                field_of_study="Computer Science",
                end_date="2020",
            )
        ],
        experience=[
            WorkExperience(
                company="ScaleFlow Technologies",
                title="Lead Infrastructure Engineer",
                start_date="2022-01",
                end_date="Present",
                is_current=True,
                description_bullets=[
                    "Architected multi-region Kubernetes clusters supporting over 200k RPS with 99.999% SLA.",
                    "Reduced AWS infrastructure costs by $340k annually through spot instances.",
                ],
                locked_facts=[
                    LockedFact(
                        category="scale",
                        statement="Architected multi-region Kubernetes clusters supporting over 200k RPS with 99.999% SLA.",
                        metric_value="200k RPS, 99.999%",
                    ),
                    LockedFact(
                        category="revenue",
                        statement="Reduced AWS infrastructure costs by $340k annually through spot instances.",
                        metric_value="$340k",
                    ),
                ],
            )
        ],
        skills=SkillSet(
            languages=["Python", "Go"],
            frameworks=["FastAPI", "Docker"],
            developer_tools=["Redis", "PostgreSQL"],
            cloud_devops=["AWS", "Kubernetes"],
            domain_knowledge=["Distributed Systems"],
        ),
        projects=[],
        years_of_experience=4.0,
    )
    prof.seal_profile()
    return prof


@pytest.fixture
def qualified_job() -> EvaluatedJob:
    """Fixture providing a qualified target job."""
    job = JobPosting(
        id="target_job_101",
        title="Senior Cloud Reliability Engineer",
        company="GlobalSaaS",
        location="Remote",
        job_url="https://globalsaas.com/jobs/101",
        description="Seeking Senior Cloud Reliability Engineer with Kubernetes, AWS, and Redis expertise.",
        is_remote=True,
        source="indeed",
    )
    score = EvaluationScore(
        embedding_similarity=0.72,
        fit_score=8.5,
        technical_score=9.0,
        seniority_score=8.0,
        passed_threshold=True,
        reasoning="Exceptional match across Kubernetes and AWS.",
        matching_skills=["Kubernetes", "AWS", "Redis"],
    )
    return EvaluatedJob(job=job, evaluation=score)


@pytest.mark.parametrize("location,width,heading", [
    ("India", 595.3, "Professional Summary"),
    ("USA", 612.0, "Professional Summary"),
    ("United Kingdom", 595.3, "Professional Profile"),
])
def test_regional_pdf_dimensions_and_source_facts(candidate_profile, qualified_job, tmp_path, location, width, heading):
    job = qualified_job.job.model_copy(update={"location": location})
    data = ResumeTailorer().generate_tailored_profile_data(candidate_profile, job)
    pdf = TypstResumeCompiler(output_dir=tmp_path).compile_resume(data, job.id)
    reader = pypdf.PdfReader(pdf)
    assert abs(float(reader.pages[0].mediabox.width) - width) < 0.2
    text = " ".join(page.extract_text() for page in reader.pages)
    assert heading.casefold() in text.casefold()
    assert candidate_profile.contact.full_name in text
    for role in candidate_profile.experience:
        assert role.company in text
        assert set(role.description_bullets) == set(next(exp["description_bullets"] for exp in data["experience"] if exp["company"] == role.company))


def test_anti_hallucination_metric_restoration(candidate_profile, qualified_job):
    """Verify that if an LLM drops a locked metric, the rewriter detects and restores it."""
    tailorer = ResumeTailorer()

    # Simulate an LLM output that stripped the numbers ($340k, 200k RPS)
    stripped_llm_output = {
        "tailored_summary": "Experienced engineer specializing in cloud.",
        "tailored_experience": [
            {
                "company": "ScaleFlow Technologies",
                "title": "Lead Infrastructure Engineer",
                "tailored_bullets": [
                    "Helped manage Kubernetes clusters for reliable web applications.",
                    "Reduced cloud costs by utilizing spot instances.",
                ],
            }
        ],
    }

    # Pass through anti-hallucination verification
    enforced = tailorer._verify_and_enforce_metric_integrity(candidate_profile, stripped_llm_output)
    bullets = enforced["tailored_experience"][0]["tailored_bullets"]

    # The original locked bullets must have been restored!
    full_text = " ".join(bullets)
    assert "$340k" in full_text
    assert "200k RPS" in full_text


def test_typst_compiler_single_pass_pdf_generation(candidate_profile, qualified_job, tmp_path: Path):
    """Verify Typst compiler compiles single-column ATS PDF in milliseconds."""
    tailorer = ResumeTailorer()
    compiler = TypstResumeCompiler(output_dir=tmp_path)

    tailored_data = tailorer.generate_tailored_profile_data(candidate_profile, qualified_job.job)
    pdf_path = compiler.compile_resume(tailored_data, job_id=qualified_job.job.id)

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 1000
    audit = json.loads(pdf_path.with_suffix(".ats.json").read_text(encoding="utf-8"))
    assert audit["passed"] is True
    assert audit["source_bullets_checked"] >= 1

    # Verify text is fully searchable and extractable by an ATS
    reader = pypdf.PdfReader(str(pdf_path))
    assert len(reader.pages) >= 1
    extracted_text = reader.pages[0].extract_text()

    assert "Alex Rivera" in extracted_text
    assert "ScaleFlow Technologies" in extracted_text
    assert "200k RPS" in extracted_text
    assert "$340k" in extracted_text


def test_resume_tailoring_pipeline_end_to_end(candidate_profile, qualified_job, tmp_path: Path):
    """Verify complete Phase 4 pipeline from qualified jobs to PDF manifest."""
    from job_agent.sourcing.delta_store import DeltaStore

    prof_path = tmp_path / "profile.json"
    prof_path.write_text(candidate_profile.model_dump_json(), encoding="utf-8")

    qual_path = tmp_path / "qualified_jobs.json"
    qual_path.write_text(json.dumps([qualified_job.model_dump()]), encoding="utf-8")

    delta_db = tmp_path / "delta.db"
    store = DeltaStore(db_path=delta_db)
    store.mark_seen(qualified_job.job, status="qualified")

    compiler = TypstResumeCompiler(output_dir=tmp_path / "resumes")
    pipeline = ResumeTailoringPipeline(compiler=compiler, delta_store=store)

    results = pipeline.run_tailoring(
        profile_path=prof_path,
        qualified_jobs_path=qual_path,
    )

    assert len(results) == 1
    assert results[0]["job_id"] == qualified_job.job.id
    assert Path(results[0]["pdf_path"]).exists()
    assert (tmp_path / "resumes" / "manifest.json").exists()

