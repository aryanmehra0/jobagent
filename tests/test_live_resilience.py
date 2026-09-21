"""Regressions for failures observed in live validation runs.

Each test reproduces a failure recorded under data/outputs/live_validation/ that
the unit suite did not catch, because it only occurs when a real model returns
imperfect output or a real quota runs out mid-batch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_agent.config.settings import settings

RESUME_TEXT = """ALEX RIVERA
alex.rivera@example.com | +1 (415) 555-0142 | San Francisco, CA

PROFESSIONAL SUMMARY
Infrastructure engineer building resilient distributed systems.

WORK EXPERIENCE
ScaleFlow Technologies - Lead Infrastructure Engineer (2022 - Present)
- Architected multi-region Kubernetes clusters supporting over 200k RPS.

CloudPulse Inc - Senior Software Engineer (2020 - 2022)
- Designed an event ingestion pipeline processing 50M events daily.

EDUCATION
Stanford University - M.S. in Computer Science (2018 - 2020)

TECHNICAL SKILLS
Languages: Python, Go, Rust
Cloud & DevOps: AWS, Kubernetes, Terraform
"""


def _llm_draft(start_date):
    """A draft a model plausibly returns: right structure, one date omitted."""
    return {
        "contact": {"full_name": "ALEX RIVERA", "email": "alex.rivera@example.com",
                    "phone": "+1 (415) 555-0142", "location": "San Francisco, CA"},
        "summary": "Infrastructure engineer building resilient distributed systems.",
        "work_authorization": {"current_country": "", "citizenship": [],
                               "authorized_countries": [], "requires_sponsorship": None},
        "education": [],
        "experience": [
            {"company": "ScaleFlow Technologies", "title": "Lead Infrastructure Engineer",
             "start_date": start_date, "end_date": "Present",
             "description_bullets": ["Architected multi-region Kubernetes clusters supporting over 200k RPS."],
             "locked_facts": []},
            {"company": "CloudPulse Inc", "title": "Senior Software Engineer",
             "start_date": "2020", "end_date": "2022",
             "description_bullets": ["Designed an event ingestion pipeline processing 50M events daily."],
             "locked_facts": []},
        ],
        "skills": {"languages": ["Python", "Go"], "frameworks": [], "developer_tools": [],
                   "cloud_devops": ["AWS"], "domain_knowledge": []},
        "projects": [], "certifications": [],
        "years_of_experience": 6.0,
    }


# ==============================================================================
# INTAKE: source reconciliation must happen before validation
# ==============================================================================

def test_groq_draft_missing_a_date_is_repaired_from_the_source(tmp_path: Path, monkeypatch):
    """Live run 112420: the model returned `start_date: null` for a role.

    The date is plainly in the resume, and the deterministic parser reads it. The
    draft was validated before that source reconciliation ran, so intake failed
    on a fact the pipeline already had. The model must not be asked to repair
    what the source text can supply directly.
    """
    import job_agent.llm as llm
    from job_agent.intake.parser import ResumeParser

    calls = []

    def fake_complete(system, prompt, **kwargs):
        calls.append(prompt)
        return _llm_draft(start_date=None)

    monkeypatch.setattr(llm, "groq_complete", fake_complete)
    monkeypatch.setattr(settings, "default_llm_provider", "groq")
    monkeypatch.setattr(settings, "llm_strict", True)

    resume = tmp_path / "resume.txt"
    resume.write_text(RESUME_TEXT, encoding="utf-8")

    profile = ResumeParser(provider="groq").parse(resume, output_path=tmp_path / "profile.json")

    lead = next(role for role in profile.experience if role.company == "ScaleFlow Technologies")
    assert lead.start_date == "2022", "the date must come from the resume text"
    assert len(calls) == 1, "a source-repairable gap must not cost a second model call"


def test_reconciliation_matches_roles_despite_minor_wording_differences(tmp_path: Path, monkeypatch):
    """Models trim or expand employer names; exact matching then finds nothing."""
    import job_agent.llm as llm
    from job_agent.intake.parser import ResumeParser

    draft = _llm_draft(start_date=None)
    draft["experience"][0]["company"] = "ScaleFlow"  # the resume says "ScaleFlow Technologies"

    monkeypatch.setattr(llm, "groq_complete", lambda *a, **k: json.loads(json.dumps(draft)))
    monkeypatch.setattr(settings, "default_llm_provider", "groq")
    monkeypatch.setattr(settings, "llm_strict", True)

    resume = tmp_path / "resume.txt"
    resume.write_text(RESUME_TEXT, encoding="utf-8")

    profile = ResumeParser(provider="groq").parse(resume, output_path=tmp_path / "profile.json")
    assert profile.experience[0].start_date == "2022"


def test_reconciliation_never_invents_a_date_the_source_lacks(tmp_path: Path, monkeypatch):
    """With no source evidence the gap must stay a gap, surfaced as an error."""
    import job_agent.llm as llm
    from job_agent.intake.parser import ResumeParser

    draft = _llm_draft(start_date=None)
    draft["experience"][0]["company"] = "Entirely Different Employer"
    draft["experience"][0]["title"] = "Chief Everything Officer"

    monkeypatch.setattr(llm, "groq_complete", lambda *a, **k: json.loads(json.dumps(draft)))
    monkeypatch.setattr(settings, "default_llm_provider", "groq")
    monkeypatch.setattr(settings, "llm_strict", True)

    resume = tmp_path / "resume.txt"
    resume.write_text(RESUME_TEXT, encoding="utf-8")

    with pytest.raises(ValueError, match="start_date"):
        ResumeParser(provider="groq").parse(resume, output_path=tmp_path / "profile.json")


# ==============================================================================
# EVALUATION: an interrupted batch must resume, not restart
# ==============================================================================

class _QuotaExhausted(RuntimeError):
    pass


def _seed_profile_and_jobs(outputs: Path, count: int):
    """A sealed profile and `count` scraped jobs, written where evaluation reads them."""
    from job_agent.config.schema import (
        CandidateProfile, ContactInfo, JobPosting, SkillSet, WorkAuthorization, WorkExperience,
    )

    outputs.mkdir(parents=True, exist_ok=True)
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Alex Rivera", email="alex@example.com", location="Remote"),
        summary="Infrastructure engineer building resilient distributed systems.",
        work_authorization=WorkAuthorization(current_country="United States"),
        experience=[WorkExperience(company="ScaleFlow", title="Lead Engineer", start_date="2020",
                                   description_bullets=["Built Kubernetes platforms on AWS."])],
        skills=SkillSet(languages=["Python"], cloud_devops=["AWS", "Kubernetes"]),
        years_of_experience=5.0,
    ).seal_profile()
    profile_path = outputs / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")

    jobs = [
        JobPosting(id=f"job{index:05d}", title="Platform Engineer", company=f"Company {index}",
                   job_url=f"https://example.com/jobs/{index}",
                   description="Kubernetes AWS Python infrastructure platform engineering role.",
                   source="greenhouse")
        for index in range(count)
    ]
    jobs_path = outputs / "scraped_jobs.json"
    jobs_path.write_text(json.dumps([job.model_dump() for job in jobs]), encoding="utf-8")
    return profile_path, jobs_path


class _CountingReranker:
    """Scores deterministically, and can be told to fail after N calls."""

    def __init__(self, fail_after=None):
        from job_agent.evaluation.reranker import LLMReranker

        self._inner = LLMReranker(provider="none")
        self.fail_after = fail_after
        self.scored_ids = []

    def evaluate_job(self, profile, job, similarity):
        if self.fail_after is not None and len(self.scored_ids) >= self.fail_after:
            raise _QuotaExhausted("Groq rate limit (HTTP 429): retry after 90s.")
        self.scored_ids.append(job.id)
        return self._inner.evaluate_job(profile, job, similarity)


def test_evaluation_resumes_after_an_interrupted_batch(monkeypatch):
    """Live run 114044: a 429 partway through discarded every score already paid for.

    Scores were checkpointed after each job, but the next run began by writing an
    empty result set over that checkpoint. Re-running must score only the jobs
    the interrupted run never reached.
    """
    from job_agent.evaluation.embedder import SemanticEmbedder
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    from job_agent.sourcing.delta_store import DeltaStore

    monkeypatch.setattr(settings, "llm_strict", True)
    outputs = settings.outputs_dir
    profile_path, jobs_path = _seed_profile_and_jobs(outputs, count=5)
    store = DeltaStore(db_path=outputs / "delta.db")

    first = _CountingReranker(fail_after=2)
    with pytest.raises(_QuotaExhausted):
        SemanticEvaluationPipeline(embedder=SemanticEmbedder(), reranker=first, delta_store=store).run_evaluation(
            profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs,
        )
    assert len(first.scored_ids) == 2

    second = _CountingReranker()
    evaluated, _ = SemanticEvaluationPipeline(
        embedder=SemanticEmbedder(), reranker=second, delta_store=store,
    ).run_evaluation(profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs)

    assert len(evaluated) == 5, "the final result must cover every job"
    assert len(second.scored_ids) == 3, "only the three unscored jobs may be re-scored"
    assert not set(second.scored_ids) & set(first.scored_ids)


def test_a_changed_profile_invalidates_the_evaluation_checkpoint(monkeypatch):
    """Scores computed against an old profile must never be reused for a new one."""
    from job_agent.config.schema import CandidateProfile
    from job_agent.evaluation.embedder import SemanticEmbedder
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    from job_agent.sourcing.delta_store import DeltaStore

    monkeypatch.setattr(settings, "llm_strict", True)
    outputs = settings.outputs_dir
    profile_path, jobs_path = _seed_profile_and_jobs(outputs, count=4)
    store = DeltaStore(db_path=outputs / "delta.db")

    first = _CountingReranker(fail_after=2)
    with pytest.raises(_QuotaExhausted):
        SemanticEvaluationPipeline(embedder=SemanticEmbedder(), reranker=first, delta_store=store).run_evaluation(
            profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs,
        )

    # Re-intake produces a different sealed profile.
    profile = CandidateProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
    profile.experience[0].description_bullets.append("Cut deploy time by 60%.")
    profile.experience[0].locked_facts = []
    from job_agent.intake.validator import enrich_locked_facts

    data = enrich_locked_facts(json.loads(profile.model_dump_json()))
    reloaded = CandidateProfile(**data).seal_profile()
    profile_path.write_text(reloaded.model_dump_json(), encoding="utf-8")

    second = _CountingReranker()
    evaluated, _ = SemanticEvaluationPipeline(
        embedder=SemanticEmbedder(), reranker=second, delta_store=store,
    ).run_evaluation(profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs)

    assert len(second.scored_ids) == 4, "every job must be re-scored against the new profile"
    assert len(evaluated) == 4


def test_a_completed_evaluation_does_not_leave_a_checkpoint(monkeypatch):
    """A finished batch is final; a later run must start fresh, not reuse it."""
    from job_agent.evaluation.embedder import SemanticEmbedder
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    from job_agent.sourcing.delta_store import DeltaStore

    outputs = settings.outputs_dir
    profile_path, jobs_path = _seed_profile_and_jobs(outputs, count=3)
    store = DeltaStore(db_path=outputs / "delta.db")

    SemanticEvaluationPipeline(
        embedder=SemanticEmbedder(), reranker=_CountingReranker(), delta_store=store,
    ).run_evaluation(profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs)

    again = _CountingReranker()
    SemanticEvaluationPipeline(
        embedder=SemanticEmbedder(), reranker=again, delta_store=store,
    ).run_evaluation(profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs)
    assert len(again.scored_ids) == 3


# ==============================================================================
# TAILORING: an interrupted batch must keep the resumes it already compiled
# ==============================================================================

class _FlakyTailorer:
    """Wraps the deterministic tailorer and fails after N successful jobs."""

    def __init__(self, fail_after=None):
        from job_agent.tailoring.rewriter import ResumeTailorer

        self._inner = ResumeTailorer(provider="none")
        self.provider = "none"
        self.fail_after = fail_after
        self.tailored_ids = []

    def generate_tailored_profile_data(self, profile, job):
        if self.fail_after is not None and len(self.tailored_ids) >= self.fail_after:
            raise _QuotaExhausted("Groq rate limit (HTTP 429): retry after 90s.")
        self.tailored_ids.append(job.id)
        return self._inner.generate_tailored_profile_data(profile, job)


def _seed_qualified(outputs: Path, count: int) -> Path:
    """Write `count` qualified jobs derived from the seeded scraped jobs."""
    from job_agent.config.schema import EvaluatedJob, EvaluationScore, JobPosting

    jobs = json.loads((outputs / "scraped_jobs.json").read_text(encoding="utf-8"))[:count]
    qualified = [
        EvaluatedJob(
            job=JobPosting(**job),
            evaluation=EvaluationScore(embedding_similarity=0.8, fit_score=9.0 - index * 0.1,
                                       reasoning="strong match"),
        ).model_dump()
        for index, job in enumerate(jobs)
    ]
    path = outputs / "qualified_jobs.json"
    path.write_text(json.dumps(qualified), encoding="utf-8")
    return path


def test_tailoring_resumes_after_an_interrupted_batch(monkeypatch):
    """In strict mode an error mid-batch raised before the manifest was written.

    Every PDF already compiled was then orphaned, and re-running paid the model
    to tailor them all again. Re-running must redo only the unfinished jobs.
    """
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    monkeypatch.setattr(settings, "llm_strict", True)
    outputs = settings.outputs_dir
    profile_path, _ = _seed_profile_and_jobs(outputs, count=3)
    qualified_path = _seed_qualified(outputs, count=3)
    store = DeltaStore(db_path=outputs / "delta.db")
    compiler = TypstResumeCompiler(output_dir=outputs / "tailored_resumes")

    first = _FlakyTailorer(fail_after=2)
    with pytest.raises(_QuotaExhausted):
        ResumeTailoringPipeline(tailorer=first, compiler=compiler, delta_store=store).run_tailoring(
            profile_path=profile_path, qualified_jobs_path=qualified_path,
        )
    assert len(first.tailored_ids) == 2

    second = _FlakyTailorer()
    results = ResumeTailoringPipeline(tailorer=second, compiler=compiler, delta_store=store).run_tailoring(
        profile_path=profile_path, qualified_jobs_path=qualified_path,
    )

    assert len(results) == 3, "the manifest must list every compiled resume"
    assert len(second.tailored_ids) == 1, "only the unfinished job may be re-tailored"
    manifest = json.loads((outputs / "tailored_resumes" / "manifest.json").read_text(encoding="utf-8"))
    assert {record["job_id"] for record in manifest} == {record["job_id"] for record in results}


def test_tailoring_does_not_reuse_a_resume_whose_pdf_changed(monkeypatch):
    """A reused resume is verified by hash; a modified PDF must be rebuilt."""
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    monkeypatch.setattr(settings, "llm_strict", True)
    outputs = settings.outputs_dir
    profile_path, _ = _seed_profile_and_jobs(outputs, count=2)
    qualified_path = _seed_qualified(outputs, count=2)
    store = DeltaStore(db_path=outputs / "delta.db")
    compiler = TypstResumeCompiler(output_dir=outputs / "tailored_resumes")

    first = _FlakyTailorer(fail_after=1)
    with pytest.raises(_QuotaExhausted):
        ResumeTailoringPipeline(tailorer=first, compiler=compiler, delta_store=store).run_tailoring(
            profile_path=profile_path, qualified_jobs_path=qualified_path,
        )

    # Tamper with the resume that was already produced.
    pdf = next((outputs / "tailored_resumes").glob("resume_*.pdf"))
    pdf.write_bytes(pdf.read_bytes() + b"\n% tampered")

    second = _FlakyTailorer()
    ResumeTailoringPipeline(tailorer=second, compiler=compiler, delta_store=store).run_tailoring(
        profile_path=profile_path, qualified_jobs_path=qualified_path,
    )
    assert len(second.tailored_ids) == 2, "a resume that no longer matches its hash must be rebuilt"
