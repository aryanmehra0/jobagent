"""Retiring the previous candidate's data when the profile becomes someone else's.

A job-search tool holds a lot that belongs to ONE candidate: the tracker of what
they applied to, the outreach drafts written in their name, the scores their
profile earned, the resumes built from their PDF, their saved country and
salary. When a new resume for a different person is loaded, none of that may
carry over: a new candidate must not inherit another person's applications,
see their scores, or send mail drafted for them.

Nothing is deleted. Files move to `outputs/history/<time>_profile_change/` and
the database is copied there before its candidate-specific tables are cleared,
so the previous candidate's records can still be recovered. Jobs themselves
(titles, companies, descriptions, contacts) are kept: they are not about anyone.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_agent.config.settings import settings

# Files and folders written for a specific candidate.
CANDIDATE_FILES = (
    "applications_tracker.xlsx", "outreach_drafts.json", "warm_contacts.json",
    # The cumulative job sheets carry each job's fit score, status and resume for the previous
    # candidate; they are rebuilt for the new one. The ledgers record what that person marked
    # as applied and which employers replied to them.
    "jobs_master.csv", "jobs_latest.csv", "applications_ready.csv", "application_pack.zip",
    "manual_applications.json", "inbox_events.json",
    # Whom this candidate chose not to see again says nothing about the next one.
    "user_skips.json",
)
CANDIDATE_DIRS = ("outreach", "cover_letters", "interview_prep")

# Database tables whose rows describe one candidate, their scores, documents or actions.
CANDIDATE_TABLES = (
    "application_events", "applications", "job_evaluation_history", "job_matches",
    "resume_artifacts", "resumes", "candidate_preferences", "candidate_profiles", "users",
    "job_evaluations", "job_resumes", "job_applications", "job_outreach",
)


def is_different_candidate(previous: Optional[str], now: Optional[str]) -> bool:
    """Whether a profile now belongs to someone else. Either side unknown means no."""
    return bool(previous and now and previous != now)


def _open_backup_file(path: Path) -> sqlite3.Connection:
    """The destination of the database backup (a seam for tests)."""
    return sqlite3.connect(str(path))


def retire_previous_candidate(outputs_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Archive and reset everything that belongs to the candidate being replaced.

    Returns what was done and what was not. `complete` is False, with the reasons
    in `problems`, if anything could not be archived or cleared: the caller must
    tell the user, because a half-finished switch leaves the previous candidate's
    data in use while looking as if it were gone.
    """
    out = Path(outputs_dir) if outputs_dir else settings.outputs_dir
    archive = out / "history" / f"{time.time_ns()}_profile_change"
    moved: List[str] = []
    problems: List[str] = []

    def move(source: Path, relative: str) -> None:
        if not source.exists():
            return
        try:
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append(relative)
        except OSError as exc:
            # Typically an Excel file held open. Say so; do not pretend it moved.
            problems.append(f"Could not archive {relative}: {exc}")

    for name in CANDIDATE_FILES:
        move(out / name, name)
    for name in CANDIDATE_DIRS:
        move(out / name, name)

    # Saved country / sponsorship / salary describe a person, not a resume.
    from job_agent.intake.preferences import preferences_path

    move(preferences_path(), "preferences.json")

    cleared = _reset_database(archive, problems)
    return {"archive": str(archive), "moved": moved, "database": cleared,
            "problems": problems, "complete": not problems}


def _reset_database(archive: Path, problems: List[str]) -> Dict[str, int]:
    """Back the jobs database up, then clear what is specific to the previous candidate.

    All or nothing: nothing is cleared unless the backup succeeded, and the clearing
    runs as one transaction, so a failure part-way changes nothing. Every failure
    is appended to `problems`.
    """
    from job_agent.storage.jobs_db import JobsDatabase

    try:
        database = JobsDatabase()
    except Exception as exc:
        problems.append(f"Could not open the jobs database: {exc}")
        return {}

    counts: Dict[str, int] = {}
    try:
        with database._connect() as conn:
            if not database.database_url:
                archive.mkdir(parents=True, exist_ok=True)
                try:
                    backup = _open_backup_file(archive / "jobs.db")
                    try:
                        conn.backup(backup)
                    finally:
                        backup.close()
                except (OSError, sqlite3.Error) as exc:
                    problems.append(f"Database backup failed ({exc}); the previous candidate's records "
                                    "were left in place rather than cleared without a copy.")
                    return {}
            for table in CANDIDATE_TABLES:
                counts[table] = conn.execute(database._sql(f"SELECT COUNT(*) AS n FROM {table}")).fetchone()["n"]
                conn.execute(database._sql(f"DELETE FROM {table}"))
            conn.execute(database._sql(
                "UPDATE jobs SET fit_score = NULL, tailored_resume = NULL, resume_check = NULL, status = 'scraped'"))
    except Exception as exc:
        problems.append(f"Could not clear the previous candidate's database records ({exc}); nothing was changed.")
        return {}
    return counts
