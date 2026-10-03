"""Loading a different person's resume must not leave the previous person's data in use."""
from __future__ import annotations

import json
import sqlite3

from job_agent.config.settings import settings
from job_agent.intake import preferences, switch
from job_agent.storage.jobs_db import JobsDatabase
from tests.test_tailoring import candidate_profile  # noqa: F401  (fixture)


def _populate(out, profile_dir):
    """Everything the previous candidate would have left behind."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "applications_tracker.xlsx").write_bytes(b"tracker of the previous candidate")
    (out / "outreach_drafts.json").write_text("[]")
    (out / "warm_contacts.json").write_text("{}")
    for folder in ("outreach", "cover_letters", "interview_prep"):
        (out / folder).mkdir()
        (out / folder / "note.txt").write_text("written for the previous candidate")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "preferences.json").write_text(json.dumps({"current_country": "India", "desired_salary": 900000}))


def _seed_database():
    database = JobsDatabase()
    now = "2026-10-01T09:00:00"
    with database._connect() as conn:
        conn.execute(database._sql(
            "INSERT INTO jobs (job_id, title, company, status, fit_score, tailored_resume, resume_check, updated_at) "
            "VALUES ('j1', 'AI Engineer', 'Acme', 'applied', 8.5, 'resume_j1.pdf', 'PASS', ?)"), (now,))
        conn.execute(database._sql("INSERT INTO job_applications (job_id, status) VALUES ('j1', 'applied')")) \
            if _has_columns(conn, "job_applications", ("job_id", "status")) else None
    return database


def _has_columns(conn, table, names):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return set(names) <= columns


def test_the_previous_candidates_data_is_archived_not_deleted(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    profile_dir = tmp_path / "profiles"
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", profile_dir / "profile.json")
    _populate(out, profile_dir)

    result = switch.retire_previous_candidate()

    # Gone from the live location...
    for name in ("applications_tracker.xlsx", "outreach_drafts.json", "warm_contacts.json",
                 "outreach", "cover_letters", "interview_prep"):
        assert not (out / name).exists(), f"{name} still holds the previous candidate's data"
    assert not preferences.preferences_path().exists(), "saved country/salary must not carry over"
    # ...but recoverable.
    archive = out / "history"
    folders = list(archive.glob("*_profile_change"))
    assert len(folders) == 1 and str(folders[0]) == result["archive"]
    assert (folders[0] / "applications_tracker.xlsx").read_bytes() == b"tracker of the previous candidate"
    assert (folders[0] / "outreach" / "note.txt").exists()
    assert json.loads((folders[0] / "preferences.json").read_text())["current_country"] == "India"
    assert set(result["moved"]) >= {"applications_tracker.xlsx", "outreach", "preferences.json"}


def test_scores_resumes_and_applications_are_reset_but_the_jobs_stay(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    out.mkdir(parents=True)
    database = _seed_database()

    result = switch.retire_previous_candidate()

    with database._connect() as conn:
        job = dict(conn.execute("SELECT * FROM jobs WHERE job_id = 'j1'").fetchone())
        left = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in switch.CANDIDATE_TABLES}
    assert job["title"] == "AI Engineer" and job["company"] == "Acme", "the job itself is not about anyone"
    assert job["fit_score"] is None and job["tailored_resume"] is None and job["status"] == "scraped", \
        "the previous candidate's score, resume and 'applied' status must not describe the new one"
    assert set(left.values()) == {0}
    # The backup still has the old row, so nothing was lost.
    backup = sqlite3.connect(next((out / "history").glob("*_profile_change")) / "jobs.db")
    assert backup.execute("SELECT fit_score FROM jobs WHERE job_id = 'j1'").fetchone()[0] == 8.5
    backup.close()
    assert "job_evaluations" in result["database"]


def test_switching_with_nothing_to_archive_is_harmless(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "outputs")
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    (tmp_path / "outputs").mkdir()
    result = switch.retire_previous_candidate()
    assert result["moved"] == []


def test_the_same_person_re_uploading_a_resume_keeps_everything():
    # Identity is by email (else name): a new version of your own resume is not a new candidate.
    assert switch.is_different_candidate("asha@example.org", "asha@example.org") is False
    assert switch.is_different_candidate("asha@example.org", "ravi@example.org") is True
    assert switch.is_different_candidate(None, "ravi@example.org") is False, "the very first profile retires nothing"
    assert switch.is_different_candidate("asha@example.org", None) is False, "an unreadable profile retires nothing"


def test_clearing_preferences_resets_them_and_keeps_the_fact_seal(monkeypatch, tmp_path, candidate_profile):
    profile_file = tmp_path / "profile.json"
    monkeypatch.setattr(settings, "profile_path", profile_file)
    candidate_profile.work_authorization.current_country = "India"
    candidate_profile.work_authorization.requires_sponsorship = True
    candidate_profile.desired_salary, candidate_profile.salary_currency = 900000, "INR"
    candidate_profile.profile_hash = candidate_profile.compute_profile_hash()
    profile_file.write_text(candidate_profile.model_dump_json())
    preferences.preferences_path().write_text(json.dumps({"current_country": "India"}))

    cleared = preferences.clear_preferences()

    assert cleared.work_authorization.current_country is None
    assert cleared.work_authorization.requires_sponsorship is None
    assert cleared.work_authorization.authorized_countries == []
    assert cleared.desired_salary is None and cleared.salary_currency is None
    assert cleared.fact_hash == candidate_profile.fact_hash and cleared.verify_integrity()
    assert cleared.profile_hash == cleared.compute_profile_hash() != candidate_profile.profile_hash
    assert not preferences.preferences_path().exists()
    assert json.loads(profile_file.read_text())["work_authorization"]["current_country"] is None
