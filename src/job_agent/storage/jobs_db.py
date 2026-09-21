"""The jobs the agent has fetched, in a database you can query.

The hosted queue's Postgres database recorded only *runs*: who asked for one and
whether it finished. The jobs themselves lived in JSON files, a CSV and the
local delta store, so a database client showed queue rows and nothing about the
search. This stores each fetched job, its contact emails and its outreach state
alongside the queue, so one connection answers "what did the agent find?".

The same tables are created on Postgres (when `DATABASE_URL` is set) and on
SQLite otherwise, so a query written against one works against the other.

This is a *view* of the artifacts, not a second source of truth: every sync
rebuilds rows from what the pipeline wrote. Clearing the outputs and re-running
cannot produce disagreement, because the sync only ever adds or updates rows and
keeps the furthest lifecycle stage a job has reached.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from job_agent.config.settings import settings
from job_agent.tracking.records import JobRecord, collect_records, promote_status

JOBS_DB_NAME = "jobs.db"

# Written once per backend. SQLite and Postgres differ only in the placeholder
# style and the timestamp default, which `_sql` rewrites.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        fingerprint TEXT,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT,
        is_remote INTEGER,
        source TEXT,
        date_posted TEXT,
        discovered_at TEXT,
        salary_min DOUBLE PRECISION,
        salary_max DOUBLE PRECISION,
        salary_currency TEXT,
        job_url TEXT,
        apply_url TEXT,
        apply_method TEXT,
        auto_apply INTEGER,
        apply_note TEXT,
        company_website TEXT,
        description TEXT,
        status TEXT NOT NULL,
        fit_score DOUBLE PRECISION,
        tailored_resume TEXT,
        resume_check TEXT,
        notes TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(fit_score)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company)",
    """
    CREATE TABLE IF NOT EXISTS job_contacts (
        job_id TEXT NOT NULL,
        email TEXT NOT NULL,
        kind TEXT,
        source TEXT,
        source_url TEXT,
        confidence INTEGER,
        PRIMARY KEY (job_id, email)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_contacts_email ON job_contacts(email)",
    """
    CREATE TABLE IF NOT EXISTS job_resumes (
        job_id TEXT PRIMARY KEY,
        file_name TEXT NOT NULL,
        file_path TEXT,
        pdf BLOB NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        resume_check TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_evaluations (
        job_id TEXT PRIMARY KEY,
        fit_score DOUBLE PRECISION,
        embedding_similarity DOUBLE PRECISION,
        technical_score DOUBLE PRECISION,
        seniority_score DOUBLE PRECISION,
        threshold_used DOUBLE PRECISION,
        passed_threshold INTEGER,
        reasoning TEXT,
        matching_skills TEXT,
        missing_skills TEXT,
        scored_by TEXT,
        evaluated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_applications (
        job_id TEXT PRIMARY KEY,
        status TEXT,
        applied INTEGER,
        channel TEXT,
        apply_url TEXT,
        steps_taken INTEGER,
        error TEXT,
        resume_used TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_profile (
        id INTEGER PRIMARY KEY,
        full_name TEXT,
        email TEXT,
        phone TEXT,
        location TEXT,
        years_of_experience DOUBLE PRECISION,
        current_country TEXT,
        authorized_countries TEXT,
        requires_sponsorship INTEGER,
        remote_worldwide INTEGER,
        desired_salary DOUBLE PRECISION,
        desired_salary_max DOUBLE PRECISION,
        salary_currency TEXT,
        skills TEXT,
        source_resume TEXT,
        profile_hash TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_parameters (
        id INTEGER PRIMARY KEY,
        target_roles TEXT,
        locations TEXT,
        onsite_countries TEXT,
        remote_only INTEGER,
        hours_old INTEGER,
        job_boards TEXT,
        country_indeed TEXT,
        min_salary DOUBLE PRECISION,
        salary_currency TEXT,
        max_results_per_board INTEGER,
        find_contacts INTEGER,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS phase_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        phase TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        duration_seconds DOUBLE PRECISION,
        summary TEXT,
        started_from TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_phase_runs_phase ON phase_runs(phase, id)",
    """
    CREATE TABLE IF NOT EXISTS job_outreach (
        job_id TEXT PRIMARY KEY,
        recipient TEXT,
        status TEXT,
        note TEXT,
        subject TEXT,
        body TEXT,
        draft_file TEXT,
        updated_at TEXT
    )
    """,
)

# One row per job with its best contact email: the view most people want.
_VIEW = """
    CREATE VIEW IF NOT EXISTS job_overview AS
    SELECT j.job_id, j.title, j.company, j.location, j.source, j.status, j.fit_score,
           j.date_posted, j.salary_min, j.salary_max, j.salary_currency,
           c.email AS contact_email, c.kind AS contact_kind, c.source AS contact_source,
           j.apply_method, j.auto_apply, j.apply_url, j.job_url, j.tailored_resume, j.resume_check,
           r.file_path AS resume_file, r.size_bytes AS resume_bytes,
           o.recipient AS outreach_to, o.status AS outreach_status, o.subject AS outreach_subject
    FROM jobs j
    LEFT JOIN job_contacts c
      ON c.job_id = j.job_id
     AND c.email = (SELECT email FROM job_contacts x WHERE x.job_id = j.job_id
                    ORDER BY CASE x.kind WHEN 'hiring' THEN 0 WHEN 'person' THEN 1
                                         WHEN 'general' THEN 2 ELSE 3 END, x.email LIMIT 1)
    LEFT JOIN job_outreach o ON o.job_id = j.job_id
    LEFT JOIN job_resumes r ON r.job_id = j.job_id
"""


class JobsDatabase:
    """Stores fetched jobs in Postgres (with `DATABASE_URL`) or SQLite."""

    def __init__(self, db_path: Optional[Path] = None, database_url: Optional[str] = None):
        self.database_url = (database_url if database_url is not None
                             else (settings.database_url if db_path is None else None))
        self.db_path = Path(db_path or settings.outputs_dir / JOBS_DB_NAME)
        if not self.database_url:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def backend(self) -> str:
        return "postgres" if self.database_url else "sqlite"

    @property
    def location(self) -> str:
        """Where the data is, for a person to read; never includes a password."""
        if not self.database_url:
            return str(self.db_path)
        from urllib.parse import urlsplit

        parts = urlsplit(self.database_url)
        host = parts.hostname or "postgres"
        port = f":{parts.port}" if parts.port else ""
        return f"postgresql://{host}{port}{parts.path}"

    # --- Connections ----------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL requires psycopg. Install psycopg[binary].") from exc

            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
            return

        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _sql(self, statement: str) -> str:
        """Adapt one statement to the active backend."""
        if not self.database_url:
            return statement
        statement = (statement.replace("?", "%s").replace(" BLOB", " BYTEA")
                     .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"))
        # Postgres has no "CREATE VIEW IF NOT EXISTS"; OR REPLACE is equivalent here.
        return statement.replace("CREATE VIEW IF NOT EXISTS", "CREATE OR REPLACE VIEW")

    def _init_db(self) -> None:
        with self._connect() as conn:
            if not self.database_url:
                conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA:
                conn.execute(self._sql(statement))
            self._migrate(conn)
            try:
                conn.execute(self._sql(_VIEW))
            except Exception:
                # A view is a convenience; the tables are what matter.
                pass

    def _migrate(self, conn) -> None:
        """Add columns introduced after a database was created."""
        added = {"resume_check": "TEXT"}
        if self.database_url:
            for column, kind in added.items():
                conn.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {column} {kind}")
            # A view's columns cannot be changed in place on Postgres.
            conn.execute("DROP VIEW IF EXISTS job_overview")
            return
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for column, kind in added.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
        # The overview view predates the column; rebuild it to include it.
        conn.execute("DROP VIEW IF EXISTS job_overview")

    # --- Writing --------------------------------------------------------------

    def sync(self, outputs_dir: Optional[Path] = None) -> Dict[str, int]:
        """Write every job the agent has fetched into the database.

        The current artifacts describe the latest sweep in full. Earlier sweeps
        survive only in the cumulative jobs CSV, because each new sweep archives
        the artifacts it replaces, so those rows are backfilled from the sheet.
        The database therefore holds every job ever found, like the CSV.

        Returns counts of jobs written, contacts stored and drafts recorded.
        """
        records = collect_records(outputs_dir)

        from datetime import datetime, timezone

        from job_agent.automation.routing import route_application

        now = datetime.now(timezone.utc).isoformat()
        stats = {"jobs": 0, "contacts": 0, "outreach": 0, "evaluations": 0, "applications": 0}

        with self._connect() as conn:
            known = {
                row["job_id"]: row["status"]
                for row in conn.execute(self._sql("SELECT job_id, status FROM jobs")).fetchall()
            }
            for record in records.values():
                job = record.job
                route = route_application(job)
                status = promote_status(known.get(job.id, ""), record.status)
                conn.execute(self._sql(_UPSERT_JOB), (
                    job.id, job.fingerprint(), job.title, job.company, job.location,
                    1 if job.is_remote else 0, job.source, job.date_posted, job.discovered_at,
                    job.salary_min, job.salary_max,
                    job.salary_currency if (job.salary_min or job.salary_max) else None,
                    job.job_url, record.apply_url or route.url, route.channel,
                    1 if route.automatable else 0, route.reason, job.company_website,
                    job.description, status, record.fit_score, record.tailored_resume,
                    record.resume_check, record.notes, now,
                ))
                stats["jobs"] += 1

                conn.execute(self._sql("DELETE FROM job_contacts WHERE job_id = ?"), (job.id,))
                for contact in job.contacts:
                    conn.execute(
                        self._sql("INSERT INTO job_contacts (job_id, email, kind, source, source_url, confidence)"
                                  " VALUES (?, ?, ?, ?, ?, ?)"),
                        (job.id, contact.email, contact.kind, contact.source,
                         contact.source_url, contact.confidence),
                    )
                    stats["contacts"] += 1

                if record.outreach:
                    draft = record.outreach
                    conn.execute(self._sql(_UPSERT_OUTREACH), (
                        job.id, draft.get("to") or None, draft.get("status"), draft.get("note"),
                        draft.get("subject"), draft.get("body"),
                        Path(draft["eml"]).name if draft.get("eml") else None,
                        draft.get("updated_at") or now,
                    ))
                    stats["outreach"] += 1

            for record in records.values():
                if record.evaluation:
                    stats["evaluations"] += self._store_evaluation(conn, record)
                if record.application:
                    stats["applications"] += self._store_application(conn, record)

            self._backfill_from_csv(conn, set(records) | set(known), now, stats, outputs_dir)
            stats["resumes"] = self._store_resumes(conn, outputs_dir, now)
            self._store_profile(conn, now)
            self._store_search_parameters(conn, now)
        return stats

    @staticmethod
    def _as_text(value: Any) -> Optional[str]:
        """Lists are stored as one readable line, not as JSON, for a SQL client."""
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return "; ".join(str(item) for item in value) or None
        return str(value)

    def _store_evaluation(self, conn, record: JobRecord) -> int:
        """The fit score with its reasoning and skill match, as Phase 3 recorded it."""
        evaluation = record.evaluation
        conn.execute(self._sql("""
            INSERT INTO job_evaluations (job_id, fit_score, embedding_similarity, technical_score,
                                         seniority_score, threshold_used, passed_threshold, reasoning,
                                         matching_skills, missing_skills, scored_by, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                fit_score = excluded.fit_score, embedding_similarity = excluded.embedding_similarity,
                technical_score = excluded.technical_score, seniority_score = excluded.seniority_score,
                threshold_used = excluded.threshold_used, passed_threshold = excluded.passed_threshold,
                reasoning = excluded.reasoning, matching_skills = excluded.matching_skills,
                missing_skills = excluded.missing_skills, scored_by = excluded.scored_by,
                evaluated_at = excluded.evaluated_at
        """), (
            record.id, evaluation.get("fit_score"), evaluation.get("embedding_similarity"),
            evaluation.get("technical_score"), evaluation.get("seniority_score"),
            evaluation.get("threshold_used"), 1 if evaluation.get("passed_threshold") else 0,
            evaluation.get("reasoning"), self._as_text(evaluation.get("matching_skills")),
            self._as_text(evaluation.get("missing_skills")), evaluation.get("scored_by"),
            evaluation.get("evaluated_at"),
        ))
        return 1

    def _store_application(self, conn, record: JobRecord) -> int:
        """What the apply phase did, including a dry run and a hand-off to manual apply."""
        outcome = record.application
        # The apply phase says "skipped" for a job it cannot submit itself; the
        # sheet and the jobs table both call that manual_apply, so this does too.
        status = "manual_apply" if outcome.get("status") == "skipped" else outcome.get("status")
        conn.execute(self._sql("""
            INSERT INTO job_applications (job_id, status, applied, channel, apply_url, steps_taken,
                                          error, resume_used, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO UPDATE SET
                status = excluded.status, applied = excluded.applied, channel = excluded.channel,
                apply_url = excluded.apply_url, steps_taken = excluded.steps_taken,
                error = excluded.error, resume_used = excluded.resume_used,
                finished_at = excluded.finished_at
        """), (
            record.id, status, 1 if outcome.get("applied") else 0, outcome.get("channel"),
            outcome.get("apply_url"), outcome.get("steps_taken"), outcome.get("error"),
            Path(outcome["pdf_path"]).name if outcome.get("pdf_path") else None, outcome.get("finished_at"),
        ))
        return 1

    def _backfill_application(self, conn, row: Dict[str, str]) -> int:
        """The apply outcome a sheet row records, when the database has none for it."""
        status = row.get("Status") or ""
        if status not in ("applied", "manual_apply", "failed", "dry_run", "skipped"):
            return 0
        cursor = conn.execute(self._sql("""
            INSERT INTO job_applications (job_id, status, applied, channel, apply_url, steps_taken,
                                          error, resume_used, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id) DO NOTHING
        """), (row["Job ID"], status, 1 if status == "applied" else 0,
               (row.get("Apply Method") or "").replace(" ", "_") or None,
               row.get("Apply URL") or None, None, row.get("Notes") or None,
               row.get("Tailored Resume") or None, None))
        return 1 if cursor.rowcount else 0

    def _store_profile(self, conn, now: str) -> None:
        """The sealed candidate profile the run worked from, as one row."""
        import json as json_module

        try:
            data = json_module.loads(settings.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        contact = data.get("contact") or {}
        auth = data.get("work_authorization") or {}
        skills = data.get("skills") or {}
        flat = [skill for group in skills.values() if isinstance(group, list) for skill in group]
        conn.execute(self._sql("""
            INSERT INTO candidate_profile (id, full_name, email, phone, location, years_of_experience,
                                           current_country, authorized_countries, requires_sponsorship,
                                           remote_worldwide, desired_salary, desired_salary_max,
                                           salary_currency, skills, source_resume, profile_hash, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                full_name = excluded.full_name, email = excluded.email, phone = excluded.phone,
                location = excluded.location, years_of_experience = excluded.years_of_experience,
                current_country = excluded.current_country,
                authorized_countries = excluded.authorized_countries,
                requires_sponsorship = excluded.requires_sponsorship,
                remote_worldwide = excluded.remote_worldwide, desired_salary = excluded.desired_salary,
                desired_salary_max = excluded.desired_salary_max, salary_currency = excluded.salary_currency,
                skills = excluded.skills, source_resume = excluded.source_resume,
                profile_hash = excluded.profile_hash, updated_at = excluded.updated_at
        """), (
            contact.get("full_name"), contact.get("email"), contact.get("phone"), contact.get("location"),
            data.get("years_of_experience"), auth.get("current_country"),
            self._as_text(auth.get("authorized_countries")),
            None if auth.get("requires_sponsorship") is None else int(bool(auth["requires_sponsorship"])),
            None if auth.get("remote_worldwide") is None else int(bool(auth["remote_worldwide"])),
            data.get("desired_salary"), data.get("desired_salary_max"), data.get("salary_currency"),
            self._as_text(flat), data.get("source_document"), data.get("profile_hash"), now,
        ))

    def _store_search_parameters(self, conn, now: str) -> None:
        """The search the run was configured with, as one row."""
        try:
            from job_agent.intake.cli import load_search_parameters

            params = load_search_parameters(settings.searches_path)
        except Exception:
            return
        conn.execute(self._sql("""
            INSERT INTO search_parameters (id, target_roles, locations, onsite_countries, remote_only,
                                           hours_old, job_boards, country_indeed, min_salary,
                                           salary_currency, max_results_per_board, find_contacts, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                target_roles = excluded.target_roles, locations = excluded.locations,
                onsite_countries = excluded.onsite_countries, remote_only = excluded.remote_only,
                hours_old = excluded.hours_old, job_boards = excluded.job_boards,
                country_indeed = excluded.country_indeed, min_salary = excluded.min_salary,
                salary_currency = excluded.salary_currency,
                max_results_per_board = excluded.max_results_per_board,
                find_contacts = excluded.find_contacts, updated_at = excluded.updated_at
        """), (
            self._as_text(params.target_domains), self._as_text(params.locations),
            self._as_text(params.onsite_countries), int(bool(params.is_remote)), params.hours_old,
            self._as_text(params.job_boards), params.country_indeed, params.min_salary,
            params.salary_currency, params.max_results_per_board, int(bool(params.find_contacts)), now,
        ))

    def record_phase_run(self, phase: str, status: str, *, run_id: Optional[str] = None,
                         started_at: Optional[str] = None, finished_at: Optional[str] = None,
                         duration_seconds: Optional[float] = None, summary: Optional[Dict[str, Any]] = None,
                         started_from: str = "cli") -> None:
        """Record one phase of one run, so the database shows the run history."""
        import json as json_module

        with self._connect() as conn:
            conn.execute(self._sql("""
                INSERT INTO phase_runs (run_id, phase, status, started_at, finished_at,
                                        duration_seconds, summary, started_from)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """), (run_id, phase, status, started_at, finished_at, duration_seconds,
                    json_module.dumps(summary, default=str) if summary else None, started_from))

    def _store_resumes(self, conn, outputs_dir: Optional[Path], now: str) -> int:
        """Keep each tailored PDF in the database, byte for byte, beside its job."""
        import hashlib
        import json as json_module

        out = Path(outputs_dir) if outputs_dir else settings.outputs_dir
        folder = out / "tailored_resumes"
        checks: Dict[str, Optional[str]] = {}
        try:
            for entry in json_module.loads((folder / "manifest.json").read_text(encoding="utf-8")):
                if isinstance(entry, dict) and entry.get("job_id"):
                    checks[entry["job_id"]] = entry.get("validation_summary")
        except (OSError, ValueError):
            pass
        existing = {row["job_id"]: row["sha256"] for row in
                    conn.execute(self._sql("SELECT job_id, sha256 FROM job_resumes")).fetchall()}
        known_jobs = {row["job_id"] for row in conn.execute(self._sql("SELECT job_id FROM jobs")).fetchall()}
        titles: Dict[str, Dict[str, Any]] = {}
        try:
            for entry in json_module.loads((folder / "manifest.json").read_text(encoding="utf-8")):
                if isinstance(entry, dict) and entry.get("job_id"):
                    titles[entry["job_id"]] = entry
        except (OSError, ValueError):
            pass
        stored = 0
        for pdf in sorted(folder.glob("resume_*.pdf")) if folder.is_dir() else []:
            job_id = pdf.stem[len("resume_"):]
            if job_id not in known_jobs:
                entry = titles.get(job_id)
                if not entry:
                    continue
                # A resume from an earlier batch whose job is only in the manifest.
                conn.execute(self._sql(_UPSERT_JOB), (
                    job_id, None, entry.get("title") or "", entry.get("company") or "", None, 0, None, None,
                    entry.get("tailored_at"), None, None, None, None, None, None, 0, None, None, None,
                    "tailored", entry.get("score"), pdf.name, entry.get("validation_summary"), None, now,
                ))
                known_jobs.add(job_id)
            data = pdf.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if existing.get(job_id) == digest:
                continue
            conn.execute(self._sql("""
                INSERT INTO job_resumes (job_id, file_name, file_path, pdf, sha256, size_bytes, resume_check, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (job_id) DO UPDATE SET
                    file_name = excluded.file_name, file_path = excluded.file_path, pdf = excluded.pdf,
                    sha256 = excluded.sha256, size_bytes = excluded.size_bytes,
                    resume_check = excluded.resume_check, updated_at = excluded.updated_at
            """), (job_id, pdf.name, str(pdf.resolve()), data, digest, len(data), checks.get(job_id), now))
            stored += 1
        return stored

    def resume_pdf(self, job_id: str) -> Optional[Dict[str, Any]]:
        """The stored tailored resume for a job: its name, bytes and check result."""
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT job_id, file_name, pdf, sha256, resume_check FROM job_resumes WHERE job_id = ?"
            ), (job_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        row["pdf"] = bytes(row["pdf"])
        return row

    def _backfill_from_csv(self, conn, present: set, now: str, stats: Dict[str, int],
                           outputs_dir: Optional[Path]) -> None:
        """Add jobs from earlier sweeps, which live only in the cumulative CSV.

        The sheet carries less than an artifact does — no description — but it
        keeps the identity, score, stage, contact email and apply route, which
        is what the database is queried for.
        """
        import csv as csv_module

        from job_agent.tracking.export import JOBS_CSV_NAME

        out = Path(outputs_dir) if outputs_dir else settings.outputs_dir
        path = out / JOBS_CSV_NAME
        if not path.is_file():
            return
        source_labels = {"Job post": "job_post", "Company website": "company_site", "Hunter.io": "hunter"}
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                all_rows = [row for row in csv_module.DictReader(handle) if row.get("Job ID")]
            rows = [row for row in all_rows if row["Job ID"] not in present]
        except (OSError, ValueError):
            return

        # An outcome recorded by an earlier run survives only in the sheet, so
        # every row's outcome is backfilled, not only rows the database lacks.
        for row in all_rows:
            stats["applications"] += self._backfill_application(conn, row)

        for row in rows:
            job_id = row["Job ID"]
            score = row.get("Fit Score") or ""
            auto = (row.get("Auto-apply Possible") or "").strip().lower().startswith("yes")
            conn.execute(self._sql(_UPSERT_JOB), (
                job_id, None, row.get("Title") or "", row.get("Company") or "", row.get("Location"),
                1 if (row.get("Remote") == "Yes") else 0, row.get("Source"), row.get("Posted") or None,
                row.get("Date Found") or None,
                float(row["Salary Min"]) if row.get("Salary Min") else None,
                float(row["Salary Max"]) if row.get("Salary Max") else None,
                row.get("Currency") or None, row.get("Job URL"), row.get("Apply URL") or None,
                (row.get("Apply Method") or "").replace(" ", "_") or None, 1 if auto else 0,
                row.get("Auto-apply Possible"), row.get("Company Website") or None, None,
                row.get("Status") or "found", float(score) if score else None,
                row.get("Tailored Resume") or None, row.get("Resume Check") or None,
                row.get("Notes") or None, now,
            ))
            stats["jobs"] += 1

            email = (row.get("HR / Careers Email") or "").strip()
            others = [item.strip() for item in (row.get("Other Emails") or "").split(";") if item.strip()]
            conn.execute(self._sql("DELETE FROM job_contacts WHERE job_id = ?"), (job_id,))
            for index, address in enumerate([email] + others):
                if not address:
                    continue
                conn.execute(
                    self._sql("INSERT INTO job_contacts (job_id, email, kind, source, source_url, confidence)"
                              " VALUES (?, ?, ?, ?, ?, ?)"),
                    (job_id, address, row.get("Email Type") if index == 0 else None,
                     source_labels.get(row.get("Email Source", ""), row.get("Email Source") or None)
                     if index == 0 else None,
                     row.get("Email Found On") or None, None),
                )
                stats["contacts"] += 1

            if row.get("Outreach To") or row.get("Cold Email Body"):
                conn.execute(self._sql(_UPSERT_OUTREACH), (
                    job_id, row.get("Outreach To") or None, None, row.get("Outreach Status") or None,
                    row.get("Cold Email Subject") or None, row.get("Cold Email Body") or None,
                    row.get("Email Draft File") or None, now,
                ))
                stats["outreach"] += 1

    # --- Reading --------------------------------------------------------------

    def jobs(self, *, limit: int = 50, status: Optional[str] = None, company: Optional[str] = None,
             with_email: bool = False, min_score: Optional[float] = None) -> List[Dict[str, Any]]:
        """Jobs from the overview, best fit first."""
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if company:
            where.append("LOWER(company) LIKE ?")
            args.append(f"%{company.lower()}%")
        if with_email:
            where.append("contact_email IS NOT NULL")
        if min_score is not None:
            where.append("fit_score >= ?")
            args.append(min_score)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        query = (f"SELECT * FROM job_overview{clause} "
                 "ORDER BY fit_score DESC NULLS LAST, company LIMIT ?")
        if not self.database_url:
            # SQLite sorts NULLs first on DESC and has no NULLS LAST.
            query = query.replace("fit_score DESC NULLS LAST", "fit_score IS NULL, fit_score DESC")
        args.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(self._sql(query), tuple(args)).fetchall()]

    def stats(self) -> Dict[str, Any]:
        """Headline numbers: totals, jobs by stage, and contact coverage."""
        with self._connect() as conn:
            total = conn.execute(self._sql("SELECT COUNT(*) AS n FROM jobs")).fetchone()
            by_status = conn.execute(
                self._sql("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            ).fetchall()
            by_source = conn.execute(
                self._sql("SELECT source, COUNT(*) AS n FROM jobs GROUP BY source")
            ).fetchall()
            with_email = conn.execute(
                self._sql("SELECT COUNT(DISTINCT job_id) AS n FROM job_contacts")
            ).fetchone()
            hiring = conn.execute(
                self._sql("SELECT COUNT(DISTINCT job_id) AS n FROM job_contacts WHERE kind = 'hiring'")
            ).fetchone()
            drafts = conn.execute(
                self._sql("SELECT COUNT(*) AS n FROM job_outreach WHERE recipient IS NOT NULL")
            ).fetchone()
            top = conn.execute(self._sql(
                "SELECT company, COUNT(*) AS n FROM jobs GROUP BY company ORDER BY n DESC, company LIMIT 5"
            )).fetchall()

        number = lambda row: int(dict(row)["n"]) if row else 0
        return {
            "backend": self.backend,
            "location": self.location,
            "jobs": number(total),
            "by_status": {dict(row)["status"]: int(dict(row)["n"]) for row in by_status},
            "by_source": {dict(row)["source"] or "unknown": int(dict(row)["n"]) for row in by_source},
            "jobs_with_email": number(with_email),
            "jobs_with_hiring_email": number(hiring),
            "outreach_drafts": number(drafts),
            "top_companies": {dict(row)["company"]: int(dict(row)["n"]) for row in top},
        }


_UPSERT_JOB = """
    INSERT INTO jobs (job_id, fingerprint, title, company, location, is_remote, source, date_posted,
                      discovered_at, salary_min, salary_max, salary_currency, job_url, apply_url,
                      apply_method, auto_apply, apply_note, company_website, description, status,
                      fit_score, tailored_resume, resume_check, notes, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (job_id) DO UPDATE SET
        fingerprint = excluded.fingerprint, title = excluded.title, company = excluded.company,
        location = excluded.location, is_remote = excluded.is_remote, source = excluded.source,
        date_posted = excluded.date_posted, salary_min = excluded.salary_min,
        salary_max = excluded.salary_max, salary_currency = excluded.salary_currency,
        job_url = excluded.job_url, apply_url = excluded.apply_url,
        apply_method = excluded.apply_method, auto_apply = excluded.auto_apply,
        apply_note = excluded.apply_note, company_website = excluded.company_website,
        description = excluded.description, status = excluded.status,
        fit_score = COALESCE(excluded.fit_score, jobs.fit_score),
        tailored_resume = COALESCE(excluded.tailored_resume, jobs.tailored_resume),
        resume_check = COALESCE(excluded.resume_check, jobs.resume_check),
        notes = COALESCE(excluded.notes, jobs.notes), updated_at = excluded.updated_at
"""

_UPSERT_OUTREACH = """
    INSERT INTO job_outreach (job_id, recipient, status, note, subject, body, draft_file, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (job_id) DO UPDATE SET
        recipient = excluded.recipient, status = excluded.status, note = excluded.note,
        subject = excluded.subject, body = excluded.body, draft_file = excluded.draft_file,
        updated_at = excluded.updated_at
"""


def record_phase(phase: str, status: str, **details: Any) -> None:
    """Record a phase run, reporting rather than raising if the database is away."""
    from rich.console import Console

    try:
        JobsDatabase().record_phase_run(phase, status, **details)
    except Exception as exc:
        Console().print(f"[dim]Run history not recorded: {exc}[/dim]")


def sync_jobs_db(outputs_dir: Optional[Path] = None, quiet: bool = True) -> Optional[Dict[str, int]]:
    """Refresh the jobs database, reporting rather than raising on failure.

    Called after each phase: a database that is unreachable must not fail a
    phase whose real work is already saved on disk.
    """
    from rich.console import Console

    try:
        stats = JobsDatabase().sync(outputs_dir)
    except Exception as exc:
        Console().print(f"[yellow]Jobs database not updated: {exc}[/yellow]")
        return None
    if not quiet:
        Console().print(f"[bold green]Jobs database updated:[/bold green] {stats['jobs']} job(s).")
    return stats
