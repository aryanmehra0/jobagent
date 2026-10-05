"""The database of fetched jobs: what it stores, and that a re-sync never loses or duplicates."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    EvaluatedJob,
    EvaluationScore,
    JobPosting,
    SkillSet,
    WorkAuthorization,
)
from job_agent.storage.jobs_db import JobsDatabase
from job_agent.storage.jobs_db import _UPSERT_JOB as _UPSERT_FOR_TEST
from job_agent.tracking.records import JobRecord
from job_agent.tracking.export import JOBS_CSV_NAME


def _job(**overrides) -> JobPosting:
    data = dict(id="j1", title="Associate Product Manager", company="Osfin.ai", location="Bengaluru, India",
                is_remote=False, job_url="https://www.linkedin.com/jobs/view/1", source="linkedin",
                description="Own the product roadmap.", salary_min=1_000_000, salary_max=1_400_000,
                salary_currency="INR")
    data.update(overrides)
    return JobPosting(**data)


@pytest.fixture
def outputs(tmp_path) -> Path:
    out = tmp_path / "outputs"
    (out / "tailored_resumes").mkdir(parents=True)
    return out


def _write_artifacts(out: Path, jobs, evaluated=None, qualified=None, manifest=None, results=None):
    (out / "scraped_jobs.json").write_text(json.dumps([j.model_dump() for j in jobs]), encoding="utf-8")
    if evaluated is not None:
        (out / "evaluated_jobs.json").write_text(json.dumps(evaluated), encoding="utf-8")
    if qualified is not None:
        (out / "qualified_jobs.json").write_text(json.dumps(qualified), encoding="utf-8")
    if manifest is not None:
        (out / "tailored_resumes" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if results is not None:
        (out / "application_results.json").write_text(json.dumps(results), encoding="utf-8")


def _db(tmp_path) -> JobsDatabase:
    return JobsDatabase(db_path=tmp_path / "jobs.db")


def _job_upsert_values(now: str):
    job = _job()
    return (
        job.id, job.fingerprint(), job.title, job.company, job.location,
        1 if job.is_remote else 0, job.work_mode, job.job_type, job.source,
        job.date_posted, job.discovered_at, job.salary_min, job.salary_max,
        job.salary_currency, job.job_url, job.apply_url, None, 0, None,
        job.company_website, job.description, "active", 1, job.discovered_at or now,
        now, None, None, None, None, None, None, now,
    )


def test_a_fetched_job_is_stored_with_its_emails_and_apply_route(outputs, tmp_path):
    job = _job(contacts=[
        {"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"},
        {"email": "info@osfin.ai", "kind": "general", "source": "company_site", "source_url": "https://osfin.ai/contact"},
    ])
    _write_artifacts(outputs, [job])
    database = _db(tmp_path)
    stats = database.sync(outputs)

    assert stats == {"jobs": 1, "contacts": 2, "outreach": 0,
                     "evaluations": 0, "applications": 0, "resumes": 0}
    row = database.jobs()[0]
    assert row["title"] == "Associate Product Manager" and row["company"] == "Osfin.ai"
    assert row["salary_min"] == 1_000_000 and row["salary_currency"] == "INR"
    # The overview shows the address worth writing to, not whichever came first.
    assert row["contact_email"] == "careers@osfin.ai" and row["contact_kind"] == "hiring"
    # LinkedIn needs a login, so the row says so rather than claiming a form.
    assert row["apply_method"] == "login_required" and row["auto_apply"] == 0
    assert row["status"] == "found"


def test_production_mode_refuses_implicit_sqlite_fallback(tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.hosted.queue import HostedQueue

    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "require_database_url", False)
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)

    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        JobsDatabase()
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        DeltaStore()
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        HostedQueue()

    assert JobsDatabase(db_path=tmp_path / "jobs.db").backend == "sqlite"


def test_scores_resumes_and_outcomes_reach_the_database(outputs, tmp_path):
    job = _job()
    evaluation = {"job": job.model_dump(), "evaluation": {"fit_score": 8.25}}
    _write_artifacts(
        outputs, [job], evaluated=[evaluation], qualified=[evaluation],
        manifest=[{"job_id": "j1", "pdf_path": str(outputs / "tailored_resumes" / "resume_j1.pdf")}],
        results={"successful": [], "failed": [{"job_id": "j1", "status": "skipped", "error": "Sign-in required."}]},
    )
    database = _db(tmp_path)
    database.sync(outputs)

    row = database.jobs()[0]
    assert row["fit_score"] == pytest.approx(8.25)
    assert row["tailored_resume"] == "resume_j1.pdf"
    assert row["status"] == "manual_apply"


def test_re_syncing_updates_rows_instead_of_duplicating_them(outputs, tmp_path):
    job = _job(contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])
    _write_artifacts(outputs, [job])
    database = _db(tmp_path)
    database.sync(outputs)
    database.sync(outputs)

    assert database.stats()["jobs"] == 1
    assert len(database.jobs()[0]["contact_email"].split(",")) == 1
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM job_contacts").fetchone()["n"] == 1


def test_a_confirmed_application_is_never_downgraded_by_a_later_sweep(outputs, tmp_path):
    job = _job()
    _write_artifacts(outputs, [job], results={"successful": [{"job_id": "j1", "status": "applied"}], "failed": []})
    database = _db(tmp_path)
    database.sync(outputs)
    assert database.jobs()[0]["status"] == "applied"

    # The next sweep re-finds the same posting and knows nothing of the outcome.
    (outputs / "application_results.json").unlink()
    database.sync(outputs)
    assert database.jobs()[0]["status"] == "applied"


def test_jobs_from_earlier_sweeps_survive_in_the_database(outputs, tmp_path):
    """A new sweep archives the artifacts it replaces; the CSV is the long record."""
    with (outputs / JOBS_CSV_NAME).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "Job ID", "Date Found", "Title", "Company", "Location", "Remote", "Source", "Posted",
            "Salary Min", "Salary Max", "Currency", "Fit Score", "Status", "HR / Careers Email",
            "Email Type", "Email Source", "Email Found On", "Other Emails", "Company Website",
            "Job URL", "Apply URL", "Apply Method", "Auto-apply Possible", "Tailored Resume",
            "Outreach To", "Outreach Status", "Cold Email Subject", "Cold Email Body",
            "Email Draft File", "Notes"])
        writer.writeheader()
        writer.writerow({"Job ID": "old1", "Title": "Product Manager", "Company": "Anupam Finserv",
                         "Location": "Mumbai", "Remote": "No", "Source": "linkedin", "Fit Score": "7.8",
                         "Status": "manual_apply", "HR / Careers Email": "chanda@anupamfinserv.com",
                         "Email Type": "person", "Email Source": "Job post", "Other Emails": "hr@anupamfinserv.com",
                         "Job URL": "https://www.linkedin.com/jobs/view/9", "Apply Method": "login required",
                         "Auto-apply Possible": "No - sign-in required", "Outreach To": "chanda@anupamfinserv.com",
                         "Cold Email Subject": "Product Manager", "Cold Email Body": "Hello",
                         "Email Draft File": "old1.eml"})

    _write_artifacts(outputs, [_job()])
    database = _db(tmp_path)
    database.sync(outputs)

    rows = {row["job_id"]: row for row in database.jobs()}
    assert set(rows) == {"j1", "old1"}
    earlier = rows["old1"]
    assert earlier["fit_score"] == pytest.approx(7.8) and earlier["status"] == "manual_apply"
    assert earlier["contact_email"] == "chanda@anupamfinserv.com"
    assert earlier["apply_method"] == "login_required" and earlier["auto_apply"] == 0
    assert earlier["outreach_to"] == "chanda@anupamfinserv.com"


def test_filters_and_stats_answer_the_questions_people_ask(outputs, tmp_path):
    with_email = _job(contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])
    without = _job(id="j2", company="Aviate", title="AI Product Manager",
                   job_url="https://www.linkedin.com/jobs/view/2")
    evaluated = [{"job": with_email.model_dump(), "evaluation": {"fit_score": 8.2}},
                 {"job": without.model_dump(), "evaluation": {"fit_score": 6.0}}]
    _write_artifacts(outputs, [with_email, without], evaluated=evaluated, qualified=[evaluated[0]])
    database = _db(tmp_path)
    database.sync(outputs)

    assert [row["job_id"] for row in database.jobs(with_email=True)] == ["j1"]
    assert [row["job_id"] for row in database.jobs(min_score=7.0)] == ["j1"]
    assert [row["job_id"] for row in database.jobs(company="avia")] == ["j2"]
    assert [row["job_id"] for row in database.jobs(status="qualified")] == ["j1"]
    # Best fit first.
    assert [row["job_id"] for row in database.jobs()] == ["j1", "j2"]

    summary = database.stats()
    assert summary["backend"] == "sqlite"
    assert summary["jobs"] == 2 and summary["jobs_with_email"] == 1 and summary["jobs_with_hiring_email"] == 1
    assert summary["by_status"] == {"qualified": 1, "evaluated": 1}
    assert summary["by_source"] == {"linkedin": 2}


def test_an_unreachable_database_never_fails_the_phase(monkeypatch, capsys):
    from job_agent.storage import jobs_db

    monkeypatch.setattr(jobs_db, "JobsDatabase", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no server")))
    assert jobs_db.sync_jobs_db() is None
    assert "not updated" in capsys.readouterr().out


def test_a_postgres_url_never_leaks_its_password(tmp_path):
    database = JobsDatabase.__new__(JobsDatabase)
    database.database_url = "postgresql://job_agent:secret@postgres:5432/job_agent"
    assert database.location == "postgresql://postgres:5432/job_agent"
    assert "secret" not in database.location
    assert database.backend == "postgres"


def test_statements_are_translated_for_postgres(tmp_path):
    database = JobsDatabase.__new__(JobsDatabase)
    database.database_url = "postgresql://host/db"
    assert database._sql("SELECT * FROM jobs WHERE job_id = ?") == "SELECT * FROM jobs WHERE job_id = %s"
    assert database._sql("CREATE VIEW IF NOT EXISTS v AS SELECT 1").startswith("CREATE OR REPLACE VIEW")

    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore.__new__(DeltaStore)
    store.database_url = "postgresql://host/db"
    assert store._sql("SELECT * FROM seen_jobs WHERE job_id = ?") == (
        "SELECT * FROM seen_jobs WHERE job_id = %s"
    )
    assert "ON CONFLICT (job_id) DO NOTHING" in store._insert_ignore(
        "seen_jobs", "(job_id)", "(job_id) VALUES (?)"
    )


def test_schema_migrations_are_visible_and_applied(tmp_path):
    database = _db(tmp_path)
    migrations = database.migrations()

    assert migrations
    assert all(row["applied"] for row in migrations)
    assert {row["name"] for row in migrations} >= {
        "legacy_candidate_columns", "canonical_job_work_mode", "normalized_candidate_job_state",
        "interview_prep_artifacts", "cover_letter_artifacts",
    }
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"] == len(migrations)


# ==============================================================================
# READING IT FROM THE TERMINAL
# ==============================================================================

def test_the_query_command_reads_but_never_writes(outputs, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    _write_artifacts(outputs, [_job(contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])])
    JobsDatabase().sync(outputs)
    runner = CliRunner()

    read = runner.invoke(cli, ["db", "query", "SELECT company, contact_email FROM job_overview"])
    assert read.exit_code == 0 and "careers@osfin.ai" in read.output

    for statement in ("DELETE FROM jobs", "DROP TABLE jobs", "UPDATE jobs SET company = 'x'"):
        blocked = runner.invoke(cli, ["db", "query", statement])
        assert blocked.exit_code != 0 and "Only SELECT" in blocked.output
    assert JobsDatabase().stats()["jobs"] == 1


def test_a_query_can_be_written_to_a_csv(outputs, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    _write_artifacts(outputs, [_job()])
    JobsDatabase().sync(outputs)
    target = tmp_path / "export" / "rows.csv"

    result = CliRunner().invoke(cli, ["db", "query", "SELECT company FROM jobs", "--csv", str(target)])
    assert result.exit_code == 0
    with target.open(encoding="utf-8-sig", newline="") as handle:
        assert [row["company"] for row in csv.DictReader(handle)] == ["Osfin.ai"]


def test_csv_export_can_be_rebuilt_from_the_database_when_artifacts_are_absent(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.export import JobsCsvExporter

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job(work_mode="hybrid", contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
                     qualified=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}])
    JobsDatabase().sync(outputs)
    for name in ("scraped_jobs.json", "evaluated_jobs.json", "qualified_jobs.json", "latest_jobs.json"):
        (outputs / name).unlink(missing_ok=True)

    path = JobsCsvExporter(outputs_dir=outputs).export()

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["Job ID"] == "j1"
    assert rows[0]["Status"] == "qualified"
    assert rows[0]["Fit Score"] == "8.2"
    assert rows[0]["Work Mode"] == "Hybrid"
    assert rows[0]["HR / Careers Email"] == "careers@osfin.ai"


def test_csv_export_preserves_database_application_state_when_results_artifact_is_absent(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.export import JobsCsvExporter

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _job()
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        qualified=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        manifest=[{"job_id": "j1", "pdf_path": str(pdf), "validation_summary": "ok"}],
        results={"successful": [{"job_id": "j1", "status": "applied", "channel": "greenhouse",
                                 "apply_url": job.apply_url, "pdf_path": str(pdf),
                                 "finished_at": "2026-09-18T11:00:00Z"}],
                 "failed": []},
    )
    JobsDatabase().sync(outputs)
    (outputs / "application_results.json").unlink()

    path = JobsCsvExporter(outputs_dir=outputs).export()

    with path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["Status"] == "applied"
    assert row["Apply URL"] == job.job_url


def test_source_jobs_can_be_rebuilt_from_normalized_source_listings(outputs, tmp_path):
    job = _job(
        description="Own billing workflows and roadmap delivery.",
        work_mode="hybrid",
        contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}],
    )
    _write_artifacts(outputs, [job])
    database = _db(tmp_path)
    database.sync(outputs)
    (outputs / "scraped_jobs.json").unlink()

    jobs = database.source_jobs()

    assert len(jobs) == 1
    assert jobs[0].id == "j1"
    assert jobs[0].description == "Own billing workflows and roadmap delivery."
    assert jobs[0].work_mode == "hybrid"
    assert jobs[0].primary_contact().email == "careers@osfin.ai"


def test_evaluation_loader_can_read_sourced_jobs_from_database(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job(description="Own billing workflows and roadmap delivery.")
    _write_artifacts(outputs, [job])
    JobsDatabase().sync(outputs)
    (outputs / "scraped_jobs.json").unlink()

    jobs = SemanticEvaluationPipeline._load_jobs(outputs / "scraped_jobs.json")

    assert [item.id for item in jobs] == ["j1"]
    assert jobs[0].description == "Own billing workflows and roadmap delivery."


def test_quality_report_rebuilds_missing_csvs_from_the_database(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.quality import quality_report

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
                     qualified=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}])
    JobsDatabase().sync(outputs)
    for name in ("scraped_jobs.json", "evaluated_jobs.json", "qualified_jobs.json", "jobs_master.csv"):
        (outputs / name).unlink(missing_ok=True)

    report = quality_report(outputs)

    assert report["jobs"]["master"] == 1
    assert (outputs / "jobs_master.csv").is_file()


def test_phase_loaders_can_rebuild_qualified_jobs_from_the_database(outputs, tmp_path, monkeypatch):
    from job_agent.automation.pipeline import AutoApplyPipeline
    from job_agent.config.settings import settings
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job(work_mode="hybrid")
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": ["Kubernetes"], "scored_by": "heuristic",
        "evaluated_at": "2026-09-18T10:00:00Z",
    }
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
                     qualified=[{"job": job.model_dump(), "evaluation": evaluation}])
    JobsDatabase().sync(outputs)
    (outputs / "qualified_jobs.json").unlink()

    tailored_records = ResumeTailoringPipeline._load_qualified(outputs / "qualified_jobs.json")
    apply_jobs, apply_scores = AutoApplyPipeline._load_qualified(outputs / "qualified_jobs.json")

    assert tailored_records[0].job.id == "j1"
    assert tailored_records[0].evaluation.fit_score == pytest.approx(8.2)
    assert tailored_records[0].evaluation.matching_skills == ["Python"]
    assert apply_jobs["j1"].work_mode == "hybrid"
    assert apply_scores["j1"] == pytest.approx(8.2)


def test_apply_loader_can_rebuild_manifest_from_resume_artifacts(outputs, tmp_path, monkeypatch):
    from job_agent.automation.pipeline import AutoApplyPipeline
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _job()
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        qualified=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        manifest=[{"job_id": "j1", "pdf_path": str(pdf), "profile_hash": "profile-v1",
                   "pdf_sha256": "sha", "validation_summary": "ok"}],
    )
    JobsDatabase().sync(outputs)
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    entries = AutoApplyPipeline._load_manifest(outputs / "tailored_resumes" / "manifest.json")

    assert entries[0]["job_id"] == "j1"
    assert entries[0]["pdf_path"] == str(pdf.resolve())
    assert entries[0]["pdf_sha256"]
    assert entries[0]["score"] == pytest.approx(8.2)


def test_tailoring_manifest_sync_refreshes_resume_artifacts(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company, "score": 8.2,
                   "pdf_path": str(pdf), "profile_hash": "profile-v1",
                   "pdf_sha256": digest, "validation_passed": True, "validation_summary": "ok"}],
    )

    ResumeTailoringPipeline._sync_database(outputs)

    entries = JobsDatabase().tailored_resumes(limit=10)
    assert entries[0]["job_id"] == "j1"
    assert entries[0]["pdf_sha256"] == digest


def test_apply_results_writer_refreshes_application_state(outputs, tmp_path, monkeypatch):
    from job_agent.automation.pipeline import AutoApplyPipeline
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
    )

    AutoApplyPipeline._write_results(
        outputs / "application_results.json",
        [{"job_id": "j1", "title": job.title, "company": job.company,
          "job_url": job.job_url, "status": "dry_run", "channel": "company_site"}],
        [],
    )

    with JobsDatabase()._connect() as conn:
        row = dict(conn.execute("SELECT job_id, current_status FROM applications").fetchone())
    assert row == {"job_id": "j1", "current_status": "dry_run"}


def test_tracking_sync_refreshes_outreach_state(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking import outreach
    from job_agent.tracking.pipeline import FallbackTrackingPipeline

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job()
    _write_artifacts(outputs, [job])
    outreach.save_drafts({
        "j1": {
            "to": "hiring@example.com",
            "status": "ready",
            "note": "Ready to send",
            "subject": "Product role",
            "body": "Hello",
            "eml": str(outputs / "outreach" / "j1.eml"),
        }
    })

    FallbackTrackingPipeline._sync_database(outputs)

    with JobsDatabase()._connect() as conn:
        row = dict(conn.execute("SELECT job_id, recipient, status FROM job_outreach").fetchone())
    assert row == {"job_id": "j1", "recipient": "hiring@example.com", "status": "ready"}


def test_interview_prep_artifacts_are_database_backed(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.interview.pipeline import InterviewPrepPipeline

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
    )
    folder = outputs / "interview_prep"
    folder.mkdir()
    guide = folder / "j1.md"
    guide.write_text("# Interview prep\n", encoding="utf-8")
    digest = hashlib.sha256(guide.read_bytes()).hexdigest()
    (folder / "j1.json").write_text(json.dumps({"job_id": "j1"}), encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(guide),
         "sha256": digest, "questions": 9}
    ]), encoding="utf-8")

    InterviewPrepPipeline._sync_database()

    rows = JobsDatabase().interview_prep_artifacts()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["sha256"] == digest
    assert rows[0]["questions"] == 9
    artifact_report = JobsDatabase().artifact_integrity_report()
    assert artifact_report["ok"] is True
    assert artifact_report["counts"]["checked"] == 1


def test_document_links_fall_back_to_database_interview_prep(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.interview.pipeline import InterviewPrepPipeline
    from job_agent.tracking.supplements import document_links

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
    )
    folder = outputs / "interview_prep"
    folder.mkdir()
    guide = folder / "j1.md"
    guide.write_text("# Interview prep\n", encoding="utf-8")
    digest = hashlib.sha256(guide.read_bytes()).hexdigest()
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(guide),
         "sha256": digest, "questions": 9}
    ]), encoding="utf-8")
    InterviewPrepPipeline._sync_database()
    manifest.unlink()

    links = document_links(outputs, profile.profile_hash)

    assert links["j1"]["Interview Prep"] == str(guide.resolve())


def test_artifact_integrity_report_flags_interview_prep_hash_mismatch(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.interview.pipeline import InterviewPrepPipeline

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    _write_artifacts(outputs, [job])
    folder = outputs / "interview_prep"
    folder.mkdir()
    guide = folder / "j1.md"
    guide.write_text("# Interview prep\n", encoding="utf-8")
    digest = hashlib.sha256(guide.read_bytes()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(guide),
         "sha256": digest, "questions": 9}
    ]), encoding="utf-8")
    InterviewPrepPipeline._sync_database()
    guide.write_text("# Changed\n", encoding="utf-8")

    report = JobsDatabase().artifact_integrity_report()

    assert report["ok"] is False
    assert report["counts"]["hash_mismatch"] == 1
    assert report["failures"][0]["artifact_id"].startswith("prep_")


def test_cover_letter_artifacts_are_database_backed(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    _write_artifacts(outputs, [job])
    folder = outputs / "cover_letters"
    folder.mkdir()
    letter = folder / "cover_j1.pdf"
    letter.write_bytes(b"%PDF-1.4\ncover\n%%EOF")
    digest = hashlib.sha256(letter.read_bytes()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(letter),
         "sha256": digest, "validated": True}
    ]), encoding="utf-8")
    database = JobsDatabase()
    database.sync(outputs)

    assert database.store_cover_letter_artifacts(outputs) == 1
    rows = database.cover_letter_artifacts()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["sha256"] == digest
    assert rows[0]["validated"] is True
    artifact_report = database.artifact_integrity_report()
    assert artifact_report["ok"] is True
    assert artifact_report["counts"]["checked"] == 1


def test_document_links_fall_back_to_database_cover_letters(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.supplements import document_links

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    _write_artifacts(outputs, [job])
    folder = outputs / "cover_letters"
    folder.mkdir()
    letter = folder / "cover_j1.pdf"
    letter.write_bytes(b"%PDF-1.4\ncover\n%%EOF")
    digest = hashlib.sha256(letter.read_bytes()).hexdigest()
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(letter),
         "sha256": digest, "validated": True}
    ]), encoding="utf-8")
    database = JobsDatabase()
    database.sync(outputs)
    database.store_cover_letter_artifacts(outputs)
    manifest.unlink()

    links = document_links(outputs, profile.profile_hash)

    assert links["j1"]["Cover Letter"] == str(letter.resolve())


def test_artifact_integrity_report_flags_cover_letter_hash_mismatch(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    job = _job()
    _write_artifacts(outputs, [job])
    folder = outputs / "cover_letters"
    folder.mkdir()
    letter = folder / "cover_j1.pdf"
    letter.write_bytes(b"%PDF-1.4\ncover\n%%EOF")
    digest = hashlib.sha256(letter.read_bytes()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps([
        {"job_id": "j1", "profile_hash": profile.profile_hash, "path": str(letter),
         "sha256": digest, "validated": True}
    ]), encoding="utf-8")
    database = JobsDatabase()
    database.sync(outputs)
    database.store_cover_letter_artifacts(outputs)
    letter.write_bytes(b"%PDF-1.4\nchanged\n%%EOF")

    report = database.artifact_integrity_report()

    assert report["ok"] is False
    assert report["counts"]["hash_mismatch"] == 1
    assert report["failures"][0]["artifact_id"].startswith("letter_")


def test_application_pack_includes_resume_artifacts_when_manifest_is_missing(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.bundle import build_application_pack, validate_application_pack

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (outputs / "tailored_resumes" / "resume_j1.ats.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company, "score": 8.2,
                   "pdf_path": str(pdf), "profile_hash": profile.profile_hash,
                   "pdf_sha256": digest, "validation_passed": True, "validation_summary": "ok"}],
    )
    JobsDatabase().sync(outputs)
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    pack = build_application_pack(outputs)

    assert validate_application_pack(pack)["resumes"] == 1


def test_web_state_can_report_evaluation_and_applications_from_the_database(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.web.state import _apply_state, _evaluate_state

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
        "evaluated_at": "2026-09-18T10:00:00Z",
    }
    _write_artifacts(
        outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        results={"successful": [{"job_id": "j1", "status": "dry_run", "channel": "company_site"}],
                 "failed": [{"job_id": "j2", "status": "skipped"}]},
    )
    JobsDatabase().sync(outputs)
    for name in ("evaluated_jobs.json", "qualified_jobs.json", "application_results.json"):
        (outputs / name).unlink()

    evaluate = _evaluate_state()
    apply = _apply_state()

    assert evaluate["status"] == "ready"
    assert evaluate["metrics"]["Scored"] == 1
    assert evaluate["metrics"]["Qualified"] == 1
    assert evaluate["top"][0]["company"] == "Osfin.ai"
    assert apply["status"] == "ready"
    assert apply["metrics"]["Dry runs"] == 1


def test_web_source_state_can_report_sourced_jobs_from_the_database(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.web.state import _source_state

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job(contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])
    _write_artifacts(outputs, [job])
    JobsDatabase().sync(outputs)
    (outputs / "scraped_jobs.json").unlink()

    state = _source_state()

    assert state["status"] == "ready"
    assert state["summary"] == "1 sourced job(s) in database"
    assert state["metrics"]["This sweep"] == 1
    assert state["metrics"]["With contact email"] == 1


def test_web_tailor_state_can_report_resume_artifacts_from_the_database(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.web.state import _tailor_state

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _job()
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        qualified=[{"job": job.model_dump(), "evaluation": {"fit_score": 8.2}}],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company, "score": 8.2,
                   "pdf_path": str(pdf), "validation_summary": "ok"}],
    )
    JobsDatabase().sync(outputs)
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    state = _tailor_state()

    assert state["status"] == "ready"
    assert state["summary"] == "1 tailored PDF(s)"
    assert state["metrics"]["PDFs"] == 1
    assert state["resumes"][0]["pdf"] == "resume_j1.pdf"


def test_run_history_job_ids_fall_back_to_database_when_artifacts_are_missing(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.web.run_history import run_job_ids

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _job()
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company,
                   "score": 8.2, "pdf_path": str(pdf)}],
    )
    JobsDatabase().sync(outputs)
    for name in ("latest_jobs.json", "evaluated_jobs.json", "qualified_jobs.json"):
        (outputs / name).unlink(missing_ok=True)
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    ids = run_job_ids({
        "source": {"status": "ok"},
        "evaluate": {"status": "ok"},
        "tailor": {"status": "ok"},
    })

    assert ids == ["j1"]


def test_tracker_resume_checks_fall_back_to_database_when_manifest_is_missing(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.tracker import EXTRA_COLUMNS, EXTRA_START, JOB_ID_COLUMN, MasterTracker

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = _job()
    _write_artifacts(
        outputs,
        [job],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company,
                   "score": 8.2, "pdf_path": str(pdf), "validation_summary": "PASS - ATS safe"}],
    )
    JobsDatabase().sync(outputs)
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    tracker = MasterTracker(tmp_path / "tracker.xlsx")
    tracker.log_application(job, match_score=8.2, status="manual_apply", cold_email="", autosave=False)
    tracker.ws.cell(row=2, column=JOB_ID_COLUMN, value="j1")
    tracker.refresh_resume_links()

    check_column = EXTRA_START + EXTRA_COLUMNS.index("Resume Check")
    assert tracker.ws.cell(row=2, column=check_column).value == "PASS - ATS safe"


def test_tracking_pipeline_uses_database_evaluations_when_qualified_json_is_missing(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.tracking.pipeline import FallbackTrackingPipeline
    from job_agent.tracking.tracker import MasterTracker

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)

    job = _job(contacts=[{"email": "careers@osfin.ai", "kind": "hiring", "source": "job_post"}])
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
        "evaluated_at": "2026-09-18T10:00:00Z",
    }
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
                     qualified=[{"job": job.model_dump(), "evaluation": evaluation}])
    JobsDatabase().sync(outputs)
    (outputs / "qualified_jobs.json").unlink()

    class _Generator:
        def generate_email(self, profile, job, fit_score=8.0):
            return "Subject: Product Manager\n\nHello."

    logged = FallbackTrackingPipeline(
        email_generator=_Generator(),
        tracker=MasterTracker(tmp_path / "tracker.xlsx"),
    ).process_fallbacks(
        qualified_jobs_path=outputs / "qualified_jobs.json",
        profile_path=profile_path,
        force_track_all=True,
    )

    assert logged[0]["job_id"] == "j1"
    assert logged[0]["score"] == pytest.approx(8.2)


# ==============================================================================
# EVERY PHASE'S OUTPUT, NOT ONLY THE JOBS
# ==============================================================================

def test_a_full_run_lands_in_the_database(outputs, tmp_path, monkeypatch):
    """Profile, search settings, scores, outcomes, resumes and run history."""
    from job_agent.config.settings import settings

    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"full_name": "Asha Verma", "email": "asha@example.org", "phone": "+91-9876543210",
                    "location": "Gurugram"},
        "summary": "Product manager.", "years_of_experience": 4.0,
        "work_authorization": {"current_country": "India", "authorized_countries": ["India"],
                               "requires_sponsorship": True, "remote_worldwide": True},
        "skills": {"languages": ["Python", "SQL"], "frameworks": ["FastAPI"]},
        "desired_salary": 1_000_000, "desired_salary_max": 1_400_000, "salary_currency": "INR",
        "source_document": "asha.pdf", "profile_hash": "abc123",
    }), encoding="utf-8")
    searches = tmp_path / "searches.yaml"
    searches.write_text(
        "target_domains: [Associate Product Manager]\nlocations: [Remote, Mumbai]\n"
        "onsite_countries: [india]\nis_remote: true\nhours_old: 48\njob_boards: [linkedin, indeed]\n"
        "country_indeed: india\nmin_salary: 800000\nsalary_currency: INR\nfind_contacts: true\n",
        encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)
    monkeypatch.setattr(settings, "searches_path", searches)

    job = _job()
    evaluation = {"embedding_similarity": 0.68, "fit_score": 8.2, "technical_score": 8.0,
                  "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
                  "reasoning": "Strong overlap with lending products.", "matching_skills": ["Python", "SQL"],
                  "missing_skills": ["Kubernetes"], "scored_by": "groq", "evaluated_at": "2026-09-18T10:00:00Z"}
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
                     qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
                     results={"successful": [], "failed": [{
                         "job_id": job.id, "status": "skipped", "channel": "login_required",
                         "apply_url": "https://www.linkedin.com/jobs/view/1", "steps_taken": 0,
                         "error": "Sign-in required.", "pdf_path": "resume_j1.pdf",
                         "finished_at": "2026-09-18T11:00:00Z"}]})

    database = _db(tmp_path)
    stats = database.sync(outputs)
    assert stats["evaluations"] == 1 and stats["applications"] == 1

    with database._connect() as conn:
        profile = dict(conn.execute("SELECT * FROM candidate_profile").fetchone())
        search = dict(conn.execute("SELECT * FROM search_parameters").fetchone())
        scored = dict(conn.execute("SELECT * FROM job_evaluations").fetchone())
        applied = dict(conn.execute("SELECT * FROM job_applications").fetchone())

    assert profile["full_name"] == "Asha Verma" and profile["current_country"] == "India"
    assert profile["requires_sponsorship"] == 1 and profile["desired_salary_max"] == 1_400_000
    assert "Python" in profile["skills"] and profile["source_resume"] == "asha.pdf"
    assert search["target_roles"] == "Associate Product Manager"
    assert search["locations"] == "Remote; Mumbai" and search["onsite_countries"] == "india"
    assert search["hours_old"] == 48 and search["remote_only"] == 1
    assert scored["fit_score"] == pytest.approx(8.2) and scored["passed_threshold"] == 1
    assert scored["matching_skills"] == "Python; SQL" and scored["missing_skills"] == "Kubernetes"
    assert "lending" in scored["reasoning"] and scored["scored_by"] == "groq"
    # "skipped" is the agent's word for "apply yourself"; the sheet and the
    # database both call it manual_apply.
    assert applied["status"] == "manual_apply" and applied["applied"] == 0
    assert applied["channel"] == "login_required" and applied["error"] == "Sign-in required."

    # Re-syncing updates rather than duplicating.
    database.sync(outputs)
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM job_evaluations").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM candidate_profile").fetchone()["n"] == 1


def test_normalized_candidate_job_state_is_queryable_without_overwriting_history(outputs, tmp_path, monkeypatch):
    """The Postgres-shaped tables separate global jobs from per-candidate state."""
    from job_agent.config.settings import settings

    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"full_name": "Asha Verma", "email": "asha@example.org"},
        "summary": "Product manager.", "years_of_experience": 4.0,
        "work_authorization": {"current_country": "India", "authorized_countries": ["India"]},
        "skills": {"languages": ["Python"], "frameworks": ["FastAPI"]},
        "source_document": "asha.pdf", "profile_hash": "profile-v1",
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)

    job = _job(work_mode="hybrid", job_type="fulltime", apply_url="https://jobs.example.com/j1")
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": ["Kubernetes"], "scored_by": "groq:openai/gpt-oss-120b",
        "scoring_version": "v1", "prompt_hash": "prompt-a", "profile_hash": "profile-v1",
        "evaluated_at": "2026-09-18T10:00:00Z",
    }
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    _write_artifacts(
        outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        manifest=[{"job_id": "j1", "pdf_path": str(pdf), "validation_summary": "ok"}],
        results={"successful": [{"job_id": "j1", "status": "applied", "channel": "greenhouse",
                                 "apply_url": job.apply_url, "pdf_path": str(pdf),
                                 "finished_at": "2026-09-18T11:00:00Z"}],
                 "failed": []},
    )
    database = _db(tmp_path)
    database.sync(outputs)

    updated = dict(evaluation, fit_score=7.9, evaluated_at="2026-09-19T10:00:00Z")
    _write_artifacts(outputs, [job], evaluated=[{"job": job.model_dump(), "evaluation": updated}],
                     qualified=[{"job": job.model_dump(), "evaluation": updated}])
    database.sync(outputs)

    with database._connect() as conn:
        global_job = dict(conn.execute("SELECT work_mode, employment_type FROM jobs WHERE job_id = 'j1'").fetchone())
        match = dict(conn.execute("SELECT * FROM job_matches WHERE candidate_id = 'asha@example.org'").fetchone())
        evaluations = [dict(row) for row in conn.execute(
            "SELECT fit_score, model_provider, model_name FROM job_evaluation_history ORDER BY created_at"
        ).fetchall()]
        application = dict(conn.execute("SELECT * FROM applications").fetchone())
        events = [dict(row) for row in conn.execute("SELECT * FROM application_events").fetchall()]
        source = dict(conn.execute("SELECT * FROM job_source_listings WHERE job_id = 'j1'").fetchone())
        artifact = dict(conn.execute("SELECT * FROM resume_artifacts WHERE job_id = 'j1'").fetchone())

    assert global_job == {"work_mode": "hybrid", "employment_type": "fulltime"}
    with database._connect() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE job_id = 'j1'").fetchone()["status"] == "active"
    assert match["fit_score"] == pytest.approx(7.9)
    assert match["state"] == "applied" and match["profile_hash"] == "profile-v1"
    assert database.jobs()[0]["fit_score"] == pytest.approx(7.9)
    assert database.jobs()[0]["status"] == "applied"
    assert [row["fit_score"] for row in evaluations] == [pytest.approx(8.2), pytest.approx(7.9)]
    assert evaluations[0]["model_provider"] == "groq"
    assert evaluations[0]["model_name"] == "openai/gpt-oss-120b"
    assert application["candidate_id"] == "asha@example.org"
    assert application["current_status"] == "applied"
    assert any(event["event_type"] == "submitted" for event in events)
    assert source["source"] == "linkedin" and "Associate Product Manager" in source["raw_payload"]
    assert artifact["storage_backend"] == "local"
    assert artifact["object_key"].endswith(".pdf")
    assert artifact["mime_type"] == "application/pdf"
    assert artifact["file_path"].endswith(".pdf") and artifact["size_bytes"] > 0
    assert Path(artifact["file_path"]).exists()
    artifact_report = database.artifact_integrity_report()
    assert artifact_report["ok"] is True
    assert artifact_report["counts"]["checked"] == 1


def test_artifact_integrity_report_flags_missing_objects(outputs, tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"full_name": "Asha Verma", "email": "asha@example.org"},
        "summary": "Product manager.",
        "skills": {"languages": ["Python"]},
        "profile_hash": "profile-v1",
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)

    job = _job()
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    _write_artifacts(
        outputs,
        [job],
        manifest=[{"job_id": "j1", "pdf_path": str(pdf), "validation_summary": "ok"}],
    )
    database = _db(tmp_path)
    database.sync(outputs)
    with database._connect() as conn:
        conn.execute(database._sql(
            "UPDATE resume_artifacts SET object_key = ?, file_path = ? WHERE job_id = ?"
        ), ("missing/resume.pdf", str(outputs / "missing.pdf"), "j1"))

    report = database.artifact_integrity_report()

    assert report["ok"] is False
    assert report["counts"]["missing"] == 1
    assert report["failures"][0]["kind"] == "missing"


def test_s3_artifact_store_put_and_get_with_prefix(monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.storage.artifacts import S3ArtifactStore

    objects = {}
    closed = {"value": False}

    class FakeBody:
        def __init__(self, data: bytes):
            self.data = data

        def read(self) -> bytes:
            return self.data

        def close(self) -> None:
            closed["value"] = True

    class FakeS3Client:
        def put_object(self, **kwargs):
            objects[(kwargs["Bucket"], kwargs["Key"])] = {
                "Body": kwargs["Body"],
                "ContentType": kwargs["ContentType"],
            }

        def get_object(self, **kwargs):
            stored = objects[(kwargs["Bucket"], kwargs["Key"])]
            return {"Body": FakeBody(stored["Body"])}

    fake_boto3 = types.SimpleNamespace(client=lambda service, endpoint_url=None: FakeS3Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(settings, "artifact_s3_bucket", "bucket")
    monkeypatch.setattr(settings, "artifact_s3_prefix", "prefix")
    monkeypatch.setattr(settings, "artifact_s3_endpoint_url", "https://s3.example.test")

    store = S3ArtifactStore()
    stored = store.put_bytes("candidate/resume.pdf", b"pdf-bytes", mime_type="application/pdf")

    assert stored.backend == "s3"
    assert stored.object_key == "prefix/candidate/resume.pdf"
    assert stored.path is None
    assert objects[("bucket", "prefix/candidate/resume.pdf")]["ContentType"] == "application/pdf"
    assert store.get_bytes(stored.object_key) == b"pdf-bytes"
    assert closed["value"] is True


def test_s3_artifact_store_maps_missing_object_to_file_not_found(monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.storage.artifacts import S3ArtifactStore

    class MissingObject(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    class FakeS3Client:
        def get_object(self, **kwargs):
            raise MissingObject()

    fake_boto3 = types.SimpleNamespace(client=lambda service, endpoint_url=None: FakeS3Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(settings, "artifact_s3_bucket", "bucket")
    monkeypatch.setattr(settings, "artifact_s3_prefix", "")

    store = S3ArtifactStore()

    with pytest.raises(FileNotFoundError):
        store.get_bytes("missing.pdf")


def test_db_artifacts_verify_command(outputs, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profile.json")
    settings.profile_path.write_text(json.dumps({
        "contact": {"full_name": "Asha Verma", "email": "asha@example.org"},
        "summary": "Product manager.",
        "skills": {"languages": ["Python"]},
        "profile_hash": "profile-v1",
    }), encoding="utf-8")
    job = _job()
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    _write_artifacts(outputs, [job], manifest=[{"job_id": "j1", "pdf_path": str(pdf)}])
    JobsDatabase().sync(outputs)

    result = CliRunner().invoke(cli, ["db", "artifacts", "verify"])

    assert result.exit_code == 0, result.output
    assert "Artifact verification" in result.output
    assert "Checked" in result.output


def test_global_jobs_keep_lifecycle_separate_from_candidate_state(outputs, tmp_path):
    job = _job()
    evaluation = {"job": job.model_dump(), "evaluation": {"fit_score": 8.25}}
    _write_artifacts(
        outputs, [job], evaluated=[evaluation], qualified=[evaluation],
        results={"successful": [], "failed": [{"job_id": "j1", "status": "skipped"}]},
    )
    database = _db(tmp_path)
    database.sync(outputs)

    with database._connect() as conn:
        job_row = dict(conn.execute("SELECT status, is_active FROM jobs WHERE job_id = 'j1'").fetchone())
        match_row = dict(conn.execute("SELECT state FROM job_matches WHERE job_id = 'j1'").fetchone())
        app_row = dict(conn.execute("SELECT current_status FROM applications WHERE job_id = 'j1'").fetchone())

    assert job_row == {"status": "active", "is_active": 1}
    assert match_row["state"] == "manual_apply"
    assert app_row["current_status"] == "manual_apply"
    assert database.jobs()[0]["status"] == "manual_apply"
    assert database.integrity_report()["checks"]["candidate_states_in_jobs_status"] == 0


def test_multi_user_matches_and_applications_are_independent(tmp_path):
    database = _db(tmp_path)
    now = "2026-09-18T10:00:00Z"
    with database._connect() as conn:
        for email, score in (("asha@example.org", 9.1), ("nithya@example.org", 6.3)):
            profile = {
                "contact": {"full_name": email.split("@")[0].title(), "email": email},
                "profile_hash": f"profile-{email}",
            }
            database._store_profile(conn, now, profile, email)
        conn.execute(database._sql(_UPSERT_FOR_TEST), _job_upsert_values(now))
        first = JobRecord(job=_job(), status="qualified", fit_score=9.1,
                          evaluation={"fit_score": 9.1, "scoring_version": "v1", "profile_hash": "profile-asha"})
        second = JobRecord(job=_job(), status="evaluated_rejected", fit_score=6.3,
                           evaluation={"fit_score": 6.3, "scoring_version": "v1", "profile_hash": "profile-nithya"})
        database._store_match(conn, first, "asha@example.org", now)
        database._store_match(conn, second, "nithya@example.org", now)
        database._store_application_current(conn, "asha@example.org", "j1", "applied",
                                            {"channel": "greenhouse"}, now)
        database._store_application_current(conn, "nithya@example.org", "j1", "manual_apply",
                                            {"channel": "manual"}, now)
        rows = [dict(row) for row in conn.execute(
            "SELECT candidate_id, fit_score, state FROM job_matches ORDER BY candidate_id"
        ).fetchall()]
        apps = [dict(row) for row in conn.execute(
            "SELECT candidate_id, current_status FROM applications ORDER BY candidate_id"
        ).fetchall()]

    assert rows == [
        {"candidate_id": "asha@example.org", "fit_score": pytest.approx(9.1), "state": "qualified"},
        {"candidate_id": "nithya@example.org", "fit_score": pytest.approx(6.3), "state": "evaluated_rejected"},
    ]
    assert apps == [
        {"candidate_id": "asha@example.org", "current_status": "applied"},
        {"candidate_id": "nithya@example.org", "current_status": "manual_apply"},
    ]
    assert database.jobs(candidate="asha@example.org")[0]["status"] == "applied"
    assert database.jobs(candidate="asha@example.org")[0]["fit_score"] == pytest.approx(9.1)
    assert database.jobs(candidate="nithya@example.org")[0]["status"] == "manual_apply"
    assert database.jobs(candidate="nithya@example.org")[0]["fit_score"] == pytest.approx(6.3)


def test_hosted_user_context_scopes_candidate_identity_for_same_email(tmp_path, monkeypatch):
    from job_agent.config.settings import settings

    database = _db(tmp_path)
    now = "2026-09-18T10:00:00Z"
    profile = {
        "contact": {"full_name": "Shared Candidate", "email": "shared@example.org"},
        "profile_hash": "profile-shared",
    }
    with database._connect() as conn:
        monkeypatch.setattr(settings, "hosted_user_id", "host-a")
        database._store_profile(conn, now, profile)
        monkeypatch.setattr(settings, "hosted_user_id", "host-b")
        database._store_profile(conn, now, profile)
        rows = [dict(row) for row in conn.execute(
            "SELECT candidate_id, user_id, email FROM candidate_profiles ORDER BY user_id"
        ).fetchall()]
        users = [dict(row) for row in conn.execute(
            "SELECT user_id, email FROM users ORDER BY user_id"
        ).fetchall()]

    assert rows == [
        {"candidate_id": "host-a__shared@example.org", "user_id": "host-a", "email": "shared@example.org"},
        {"candidate_id": "host-b__shared@example.org", "user_id": "host-b", "email": "shared@example.org"},
    ]
    assert users == [
        {"user_id": "host-a", "email": "shared@example.org"},
        {"user_id": "host-b", "email": "shared@example.org"},
    ]


def test_application_status_changes_append_events(tmp_path):
    database = _db(tmp_path)
    now = "2026-09-18T10:00:00Z"
    with database._connect() as conn:
        database._store_profile(conn, now, {"contact": {"email": "asha@example.org"}}, "asha@example.org")
        conn.execute(database._sql(_UPSERT_FOR_TEST), _job_upsert_values(now))
        database._store_application_current(conn, "asha@example.org", "j1", "applied", {}, now)
        database._store_application_current(conn, "asha@example.org", "j1", "interview", {},
                                            "2026-09-19T10:00:00Z")
        database._store_application_current(conn, "asha@example.org", "j1", "offer", {},
                                            "2026-09-20T10:00:00Z")
        app = dict(conn.execute("SELECT current_status FROM applications").fetchone())
        events = [dict(row) for row in conn.execute(
            "SELECT event_type, from_status, to_status FROM application_events ORDER BY created_at"
        ).fetchall()]

    assert app["current_status"] == "offer"
    assert [event["to_status"] for event in events] == ["applied", "interview", "offer"]
    assert events[1]["from_status"] == "applied"


def test_the_run_history_is_recorded_phase_by_phase(tmp_path):
    database = _db(tmp_path)
    database.record_phase_run("source", "ok", run_id="r1", started_at="2026-09-18T10:00:00Z",
                              finished_at="2026-09-18T10:12:00Z", duration_seconds=720.5,
                              summary={"found": 379}, started_from="dashboard")
    database.record_phase_run("evaluate", "cancelled", run_id="r1", duration_seconds=12.0,
                              started_from="dashboard")

    with database._connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM phase_runs ORDER BY id")]
    assert [row["phase"] for row in rows] == ["source", "evaluate"]
    assert rows[0]["status"] == "ok" and rows[0]["duration_seconds"] == pytest.approx(720.5)
    assert json.loads(rows[0]["summary"])["found"] == 379
    assert rows[1]["status"] == "cancelled" and rows[1]["run_id"] == "r1"
    events = database.run_events(run_id="r1")
    assert {event["event_type"] for event in events} == {"phase_ok", "phase_cancelled"}
    source = next(event for event in events if event["phase"] == "source")
    assert source["success"] is True
    assert source["latency_ms"] == 720500
    assert source["metadata"]["summary"] == {"found": 379}


def test_structured_run_events_are_append_only_and_queryable(tmp_path):
    database = _db(tmp_path)
    event_id = database.record_run_event(
        run_id="r1",
        candidate_id="asha@example.org",
        job_id="j1",
        phase="llm",
        event_type="groq_complete",
        success=True,
        latency_ms=123,
        metadata={"provider": "groq", "prompt_hash": "abc"},
    )

    rows = database.run_events(run_id="r1", phase="llm")

    assert rows[0]["event_id"] == event_id
    assert rows[0]["success"] is True
    assert rows[0]["metadata"] == {"provider": "groq", "prompt_hash": "abc"}


def test_db_runs_command_reads_database_run_history(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    database = JobsDatabase()
    database.save_run({
        "run_id": "run-1",
        "started_at": "2026-09-18T10:00:00Z",
        "finished_at": "2026-09-18T10:05:00Z",
        "status": "ok",
        "candidate_name": "Asha Verma",
        "candidate_key": "asha",
        "dry_run": True,
        "started_from": "cli",
    })
    database.link_run_jobs("run-1", ["j1", "j2"])
    json_out = tmp_path / "runs.json"

    result = CliRunner().invoke(cli, ["db", "runs", "--candidate", "asha", "--json-out", str(json_out)])

    assert result.exit_code == 0, result.output
    assert "run-1" in result.output
    assert "ok" in result.output
    exported = json.loads(json_out.read_text(encoding="utf-8"))
    assert exported[0]["run_id"] == "run-1"
    assert exported[0]["job_count"] == 2


def test_a_cli_phase_records_itself_in_the_run_history(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    # `track` refuses to run without a sealed profile at settings.profile_path.
    # Point it at an isolated, sealed fixture rather than relying on whatever
    # profile.json a developer happens to have on disk — that ambient
    # dependency is exactly what let this test pass locally while failing on
    # a clean CI checkout with no data/profiles/profile.json at all.
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    _write_artifacts(outputs, [_job()])
    result = CliRunner().invoke(cli, ["export"])
    assert result.exit_code == 0

    with JobsDatabase()._connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM phase_runs")]
    assert rows == []          # export is not a phase

    result = CliRunner().invoke(cli, ["track", "--all"])
    assert result.exit_code == 0, f"track --all failed: {result.output}"
    with JobsDatabase()._connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT phase, status, started_from FROM phase_runs")]
    assert rows and rows[-1]["phase"] == "track" and rows[-1]["started_from"] == "cli"


def test_tailor_cli_allows_database_fallback_when_qualified_artifact_is_missing(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)

    called = {}

    def fake_run_tailoring(self, **kwargs):
        called.update(kwargs)
        return []

    monkeypatch.setattr(ResumeTailoringPipeline, "run_tailoring", fake_run_tailoring)

    result = CliRunner().invoke(cli, ["tailor"])

    assert result.exit_code == 0, result.output
    assert called["qualified_jobs_path"] == outputs / "qualified_jobs.json"
    assert not called["qualified_jobs_path"].exists()


def test_evaluate_cli_allows_database_fallback_when_scraped_artifact_is_missing(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)

    called = {}

    def fake_run_evaluation(self, **kwargs):
        called.update(kwargs)
        return [], []

    monkeypatch.setattr(SemanticEvaluationPipeline, "run_evaluation", fake_run_evaluation)

    result = CliRunner().invoke(cli, ["evaluate"])

    assert result.exit_code == 0, result.output
    assert called["jobs_path"] == outputs / "scraped_jobs.json"
    assert not called["jobs_path"].exists()


def test_evaluation_output_writer_refreshes_database_state(monkeypatch, outputs):
    from job_agent.config.settings import settings
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job()
    _write_artifacts(outputs, [job])
    evaluated = EvaluatedJob(
        job=job,
        evaluation=EvaluationScore(
            embedding_similarity=0.72,
            fit_score=8.4,
            technical_score=8.0,
            seniority_score=7.5,
            threshold_used=7.0,
            reasoning="Strong match.",
            matching_skills=["product"],
            missing_skills=[],
            scored_by="heuristic",
        ),
    )

    SemanticEvaluationPipeline._write_outputs(outputs, [evaluated], [evaluated])

    rows = JobsDatabase().evaluated_jobs()
    assert len(rows) == 1
    assert rows[0].job.id == job.id
    assert rows[0].evaluation.fit_score == pytest.approx(8.4)


def test_status_cli_reports_database_sourced_jobs_when_scraped_artifact_is_missing(monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    job = _job()
    _write_artifacts(outputs, [job])
    JobsDatabase().sync(outputs)
    (outputs / "scraped_jobs.json").unlink()

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "1 sourced job(s) in database" in result.output


def test_apply_cli_allows_database_fallback_when_manifest_artifact_is_missing(monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.automation.pipeline import AutoApplyPipeline
    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)

    called = {}

    def fake_run_applications(self, **kwargs):
        called.update(kwargs)
        return [], []

    monkeypatch.setattr(AutoApplyPipeline, "run_applications", fake_run_applications)

    result = CliRunner().invoke(cli, ["apply", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert called["dry_run"] is True
    assert not (outputs / "tailored_resumes" / "manifest.json").exists()


def test_workday_assist_cli_uses_database_qualified_and_manifest_fallbacks(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    import job_agent.automation.agent as agent_module
    from job_agent.cli import cli
    from job_agent.config.settings import settings

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    pdf = outputs / "tailored_resumes" / "resume_j1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (outputs / "tailored_resumes" / "resume_j1.ats.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    job = _job(job_url="https://wd5.myworkdayjobs.com/acme/job/1", apply_url="https://wd5.myworkdayjobs.com/acme/job/1")
    evaluation = {
        "embedding_similarity": 0.61, "fit_score": 8.2, "technical_score": 8.0,
        "seniority_score": 7.5, "threshold_used": 7.0, "passed_threshold": True,
        "reasoning": "Strong product overlap.", "matching_skills": ["Python"],
        "missing_skills": [], "scored_by": "heuristic",
    }
    _write_artifacts(
        outputs,
        [job],
        evaluated=[{"job": job.model_dump(), "evaluation": evaluation}],
        qualified=[{"job": job.model_dump(), "evaluation": evaluation}],
        manifest=[{"job_id": "j1", "title": job.title, "company": job.company, "score": 8.2,
                   "pdf_path": str(pdf), "profile_hash": profile.profile_hash,
                   "pdf_sha256": digest, "validation_passed": True, "validation_summary": "ok"}],
    )
    JobsDatabase().sync(outputs)
    (outputs / "qualified_jobs.json").unlink()
    (outputs / "tailored_resumes" / "manifest.json").unlink()

    called = {}

    class _Agent:
        def apply_to_job(self, profile, job, pdf_resume_path, assist_workday=False, review_callback=None, **kwargs):
            called["job_id"] = job.id
            called["pdf"] = pdf_resume_path
            called["assist"] = assist_workday
            return {"status": "dry_run"}

        def close(self):
            called["closed"] = True

    monkeypatch.setattr(agent_module, "AutoApplyAgent", _Agent)

    result = CliRunner().invoke(cli, ["workday-assist", "--job-id", "j1"])

    assert result.exit_code == 0, result.output
    assert called == {"job_id": "j1", "pdf": pdf, "assist": True, "closed": True}


def test_db_audit_cross_checks_artifacts(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager.",
        work_authorization=WorkAuthorization(current_country="India", authorized_countries=["India"]),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "outputs_dir", outputs)
    monkeypatch.setattr(settings, "profile_path", profile_path)
    _write_artifacts(outputs, [_job()])
    assert CliRunner().invoke(cli, ["export", "--bundle"]).exit_code == 0

    result = CliRunner().invoke(cli, ["db", "audit"])
    assert result.exit_code == 0, result.output
    assert "Database Matches Master Csv" in result.output
    report = json.loads((outputs / "audit_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["database"]["counts"]["jobs"] == report["artifacts"]["jobs_master_rows"] == 1
