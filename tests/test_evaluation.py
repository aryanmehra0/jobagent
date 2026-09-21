"""Unit and integration tests for Phase 3: Semantic Evaluation."""

from pathlib import Path
import pytest

from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    WorkAuthorization,
    Education,
    WorkExperience,
    SkillSet,
    JobPosting,
    EvaluationScore,
)
from job_agent.evaluation.embedder import SemanticEmbedder
from job_agent.evaluation.reranker import LLMReranker
from job_agent.evaluation.pipeline import SemanticEvaluationPipeline


@pytest.fixture
def candidate_profile() -> CandidateProfile:
    """Fixture providing a test candidate profile."""
    prof = CandidateProfile(
        contact=ContactInfo(
            full_name="Alex Rivera",
            email="alex.rivera@example.com",
            location="San Francisco, CA",
        ),
        summary="Senior Distributed Systems and Cloud Engineer specializing in Kubernetes, AWS, and microservices.",
        work_authorization=WorkAuthorization(
            citizenship=["United States"],
            current_country="United States",
            authorized_countries=["United States"],
        ),
        education=[
            Education(
                institution="Stanford",
                degree="M.S.",
                field_of_study="Computer Science",
            )
        ],
        experience=[
            WorkExperience(
                company="ScaleTech",
                title="Senior Distributed Systems Engineer",
                start_date="2020",
                description_bullets=[
                    "Architected multi-region Kubernetes clusters handling 200k RPS.",
                    "Optimized database latency by 45% using Redis caching.",
                ],
            )
        ],
        skills=SkillSet(
            languages=["Python", "Go", "Rust"],
            frameworks=["FastAPI", "gRPC", "Docker"],
            developer_tools=["PostgreSQL", "Redis", "Kafka"],
            cloud_devops=["AWS", "Kubernetes", "Terraform"],
            domain_knowledge=["Distributed Systems", "High Availability"],
        ),
        years_of_experience=4.5,
    )
    prof.seal_profile()
    return prof


@pytest.fixture
def sample_jobs() -> list[JobPosting]:
    """Fixture providing a highly relevant job and an irrelevant job."""
    relevant_job = JobPosting(
        id="rel_001",
        title="Senior Cloud Infrastructure Engineer",
        company="NexScale",
        location="Remote",
        job_url="https://nexscale.com/jobs/1",
        description=(
            "We are seeking a Senior Cloud Infrastructure Engineer with 4+ years experience. "
            "Must have hands-on expertise with AWS, Kubernetes, Terraform, and Python or Go. "
            "You will scale distributed cloud infrastructure supporting millions of users."
        ),
        is_remote=True,
        source="linkedin",
    )
    irrelevant_job = JobPosting(
        id="irrel_002",
        title="Junior Graphic Designer",
        company="CreativeArt Studio",
        location="New York, NY",
        job_url="https://creativeart.com/jobs/2",
        description=(
            "Looking for a junior graphic designer skilled in Adobe Photoshop, Figma, "
            "and illustration. 1 year experience required. Creating social media banners."
        ),
        is_remote=False,
        source="indeed",
    )
    return [relevant_job, irrelevant_job]


def test_embedder_similarity_differentiation(candidate_profile, sample_jobs):
    """Verify that Tier 1 embedding assigns higher similarity to relevant jobs."""
    embedder = SemanticEmbedder()
    cand_text = embedder.candidate_to_text(candidate_profile)
    job_texts = [embedder.job_to_text(j) for j in sample_jobs]

    scores = embedder.compute_similarity(cand_text, job_texts)
    assert len(scores) == 2
    rel_score, irrel_score = scores[0], scores[1]

    # Relevant job must score significantly higher than graphic designer
    assert rel_score > irrel_score
    assert rel_score >= 0.15


def test_embedder_filter_and_rank(candidate_profile, sample_jobs):
    """Verify filter_and_rank returns jobs in descending score order above threshold."""
    embedder = SemanticEmbedder()
    ranked = embedder.filter_and_rank(candidate_profile, sample_jobs, threshold=0.15)

    assert len(ranked) >= 1
    assert ranked[0][0].id == "rel_001"


def test_reranker_sliding_window_chunking():
    """Verify sliding window chunker breaks lengthy text cleanly."""
    reranker = LLMReranker()
    long_desc = "Kubernetes AWS architecture " * 300
    chunks = reranker._chunk_job_description(long_desc, window_size=500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 600


def test_reranker_fit_scoring(candidate_profile, sample_jobs):
    """Verify Tier 2 LLM judge scores relevant job >= 7.0 and irrelevant job < 7.0."""
    reranker = LLMReranker()

    score_rel = reranker.evaluate_job(candidate_profile, sample_jobs[0], embedding_similarity=0.65)
    assert score_rel.fit_score >= 7.0
    assert score_rel.passed_threshold is True
    assert "Kubernetes" in score_rel.matching_skills or "Aws" in score_rel.matching_skills

    score_irrel = reranker.evaluate_job(candidate_profile, sample_jobs[1], embedding_similarity=0.05)
    assert score_irrel.fit_score < 7.0
    assert score_irrel.passed_threshold is False


def test_pipeline_end_to_end(candidate_profile, sample_jobs, tmp_path: Path):
    """Verify complete Phase 3 pipeline execution and output artifact generation."""
    import json
    from job_agent.sourcing.delta_store import DeltaStore

    # Setup temp paths
    prof_file = tmp_path / "profile.json"
    with open(prof_file, "w", encoding="utf-8") as f:
        f.write(candidate_profile.model_dump_json())

    jobs_file = tmp_path / "scraped_jobs.json"
    with open(jobs_file, "w", encoding="utf-8") as f:
        json.dump([j.model_dump() for j in sample_jobs], f)

    delta_db = tmp_path / "delta.db"
    store = DeltaStore(db_path=delta_db)
    store.mark_many_seen(sample_jobs)

    pipeline = SemanticEvaluationPipeline(delta_store=store)
    evaluated, qualified = pipeline.run_evaluation(
        profile_path=prof_file,
        jobs_path=jobs_file,
        # Exercise Tier 2 for both postings regardless of the embedding backend.
        tier1_threshold=0.0,
        output_dir=tmp_path,
    )

    assert len(evaluated) == 2
    assert len(qualified) == 1
    assert qualified[0].job.id == "rel_001"
    assert (tmp_path / "evaluated_jobs.json").exists()
    assert (tmp_path / "qualified_jobs.json").exists()

