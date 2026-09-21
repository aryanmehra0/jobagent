"""The database of fetched jobs: what it stores, and that a re-sync never loses or duplicates."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from job_agent.config.schema import JobPosting
from job_agent.storage.jobs_db import JobsDatabase
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


def test_a_cli_phase_records_itself_in_the_run_history(tmp_path, monkeypatch, outputs):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings

    monkeypatch.setattr(settings, "outputs_dir", outputs)
    _write_artifacts(outputs, [_job()])
    result = CliRunner().invoke(cli, ["export"])
    assert result.exit_code == 0

    with JobsDatabase()._connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM phase_runs")]
    assert rows == []          # export is not a phase

    result = CliRunner().invoke(cli, ["track", "--all"])
    with JobsDatabase()._connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT phase, status, started_from FROM phase_runs")]
    assert rows and rows[-1]["phase"] == "track" and rows[-1]["started_from"] == "cli"
