"""The jobs the agent has fetched, in a database you can query.

The hosted queue's Postgres database recorded only *runs*: who asked for one and
whether it finished. The jobs themselves lived in JSON files, a CSV and the
local delta store, so a database client showed queue rows and nothing about the
search. This stores each fetched job, its contact emails and its outreach state
alongside the queue, so one connection answers "what did the agent find?".

The same tables are created on Postgres (when `DATABASE_URL` is set) and on
SQLite otherwise, so a query written against one works against the other.

The legacy tables and overview view are still kept for the local dashboard and
CLI. New code is dual-written into normalized, candidate-owned tables so the
database can become the operational source of truth without breaking existing
artifact-driven phases in one migration.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from job_agent.config.settings import settings
from job_agent.storage.migrations import latest_version, migration_status, run_migrations
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
        work_mode TEXT,
        employment_type TEXT,
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
    "CREATE INDEX IF NOT EXISTS idx_jobs_active_posted ON jobs(status, date_posted)",
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        email TEXT,
        full_name TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_profiles (
        candidate_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(user_id),
        profile_hash TEXT,
        full_name TEXT,
        email TEXT,
        phone TEXT,
        location TEXT,
        years_of_experience DOUBLE PRECISION,
        current_country TEXT,
        authorized_countries TEXT,
        requires_sponsorship INTEGER,
        remote_worldwide INTEGER,
        skills TEXT,
        source_resume TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidate_profiles_user ON candidate_profiles(user_id)",
    """
    CREATE TABLE IF NOT EXISTS candidate_preferences (
        candidate_id TEXT PRIMARY KEY REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        desired_salary DOUBLE PRECISION,
        desired_salary_max DOUBLE PRECISION,
        salary_currency TEXT,
        target_roles TEXT,
        locations TEXT,
        onsite_countries TEXT,
        work_modes TEXT,
        remote_only INTEGER,
        hours_old INTEGER,
        job_boards TEXT,
        country_indeed TEXT,
        min_salary DOUBLE PRECISION,
        max_results_per_board INTEGER,
        find_contacts INTEGER,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS resumes (
        resume_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        object_key TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        version INTEGER,
        kind TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_resumes_candidate ON resumes(candidate_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS job_source_listings (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        source_job_id TEXT,
        source_url TEXT,
        apply_url TEXT,
        raw_payload TEXT,
        first_seen_at TEXT,
        last_seen_at TEXT,
        source_posted_at TEXT,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_source_identity ON job_source_listings(source, source_job_id)",
    "CREATE INDEX IF NOT EXISTS idx_job_source_job_seen ON job_source_listings(job_id, last_seen_at)",
    """
    CREATE TABLE IF NOT EXISTS job_matches (
        match_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        state TEXT NOT NULL,
        fit_score DOUBLE PRECISION,
        technical_score DOUBLE PRECISION,
        seniority_score DOUBLE PRECISION,
        semantic_score DOUBLE PRECISION,
        matching_skills TEXT,
        missing_skills TEXT,
        scoring_version TEXT,
        prompt_hash TEXT,
        model TEXT,
        profile_hash TEXT,
        tailored_resume TEXT,
        resume_check TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_matches_current ON job_matches(candidate_id, job_id, scoring_version, profile_hash)",
    "CREATE INDEX IF NOT EXISTS idx_job_matches_lookup ON job_matches(candidate_id, state, fit_score)",
    """
    CREATE TABLE IF NOT EXISTS job_evaluation_history (
        evaluation_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        profile_hash TEXT,
        embedding_similarity DOUBLE PRECISION,
        technical_score DOUBLE PRECISION,
        seniority_score DOUBLE PRECISION,
        fit_score DOUBLE PRECISION,
        model_provider TEXT,
        model_name TEXT,
        model_version TEXT,
        prompt_hash TEXT,
        scoring_version TEXT,
        threshold DOUBLE PRECISION,
        passed INTEGER,
        reasoning TEXT,
        matching_skills TEXT,
        missing_skills TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_eval_history_job ON job_evaluation_history(candidate_id, job_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS applications (
        application_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        current_status TEXT NOT NULL,
        channel TEXT,
        apply_url TEXT,
        resume_used TEXT,
        submitted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_candidate_job ON applications(candidate_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(candidate_id, current_status, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS application_events (
        event_id TEXT PRIMARY KEY,
        application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        source TEXT,
        metadata TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_application_events_timeline ON application_events(application_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS resume_artifacts (
        artifact_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        file_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        resume_check TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_resume_artifacts_candidate_job ON resume_artifacts(candidate_id, job_id)",
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
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT,
        finished_at TEXT,
        status TEXT,
        candidate_name TEXT,
        candidate_key TEXT,
        profile_hash TEXT,
        resume_file TEXT,
        dry_run INTEGER,
        tailoring_mode TEXT,
        started_from TEXT,
        phases TEXT,
        results TEXT,
        warnings TEXT,
        totals TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)",
    """
    CREATE TABLE IF NOT EXISTS run_jobs (
        run_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        PRIMARY KEY (run_id, job_id)
    )
    """,
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
    SELECT j.job_id, j.title, j.company, j.location, j.work_mode, j.employment_type, j.source,
           COALESCE(a.current_status, m.state, j.status) AS status,
           COALESCE(m.fit_score, j.fit_score) AS fit_score,
           j.date_posted, j.salary_min, j.salary_max, j.salary_currency,
           c.email AS contact_email, c.kind AS contact_kind, c.source AS contact_source,
           j.apply_method, j.auto_apply, j.apply_url, j.job_url, j.tailored_resume, j.resume_check,
           COALESCE(ra.file_path, r.file_path) AS resume_file,
           COALESCE(ra.size_bytes, r.size_bytes) AS resume_bytes,
           o.recipient AS outreach_to, o.status AS outreach_status, o.subject AS outreach_subject
    FROM jobs j
    LEFT JOIN job_matches m
      ON m.match_id = (SELECT x.match_id FROM job_matches x WHERE x.job_id = j.job_id
                       ORDER BY x.updated_at DESC, x.created_at DESC LIMIT 1)
    LEFT JOIN applications a
      ON a.application_id = (SELECT x.application_id FROM applications x WHERE x.job_id = j.job_id
                             ORDER BY x.updated_at DESC, x.created_at DESC LIMIT 1)
    LEFT JOIN job_contacts c
      ON c.job_id = j.job_id
     AND c.email = (SELECT email FROM job_contacts x WHERE x.job_id = j.job_id
                    ORDER BY CASE x.kind WHEN 'hiring' THEN 0 WHEN 'person' THEN 1
                                         WHEN 'general' THEN 2 ELSE 3 END, x.email LIMIT 1)
    LEFT JOIN job_outreach o ON o.job_id = j.job_id
    LEFT JOIN job_resumes r ON r.job_id = j.job_id
    LEFT JOIN resume_artifacts ra
      ON ra.artifact_id = (SELECT x.artifact_id FROM resume_artifacts x WHERE x.job_id = j.job_id
                           ORDER BY x.created_at DESC LIMIT 1)
"""


# SQLite files whose schema this process has already created: {path: file identity}.
_PREPARED_FILES: Dict[str, Optional[int]] = {}
_PG_POOLS: Dict[str, Any] = {}


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

            pool = self._postgres_pool(dict_row)
            if pool is not None:
                with pool.connection() as conn:
                    with conn:
                        yield conn
                return

            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
            return

        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _postgres_pool(self, row_factory: Any) -> Optional[Any]:
        """Reuse Postgres connections when psycopg_pool is installed."""
        if not self.database_url:
            return None
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            return None
        pool = _PG_POOLS.get(self.database_url)
        if pool is None:
            pool = ConnectionPool(
                self.database_url, kwargs={"row_factory": row_factory},
                min_size=1, max_size=4, open=False,
            )
            pool.open()
            _PG_POOLS[self.database_url] = pool
        return pool

    def _sql(self, statement: str) -> str:
        """Adapt one statement to the active backend."""
        if not self.database_url:
            return statement
        statement = (statement.replace("?", "%s").replace(" BLOB", " BYTEA")
                     .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"))
        # Postgres has no "CREATE VIEW IF NOT EXISTS"; OR REPLACE is equivalent here.
        return statement.replace("CREATE VIEW IF NOT EXISTS", "CREATE OR REPLACE VIEW")

    def _file_identity(self) -> Optional[int]:
        try:
            return self.db_path.stat().st_ino
        except OSError:
            return None

    def _init_db(self) -> None:
        # Creating the schema and migrating costs about 25 ms, and a database object is made
        # for nearly every call (13 in one trivial run). A SQLite file already prepared by
        # this process is skipped; a deleted or replaced file has a new identity and is
        # prepared again. Postgres is always checked.
        if not self.database_url:
            identity = self._file_identity()
            if identity is not None and _PREPARED_FILES.get(str(self.db_path)) == identity:
                return
        with self._connect() as conn:
            if not self.database_url:
                conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA:
                conn.execute(self._sql(statement))
            self._migrate(conn)
            run_migrations(conn, self._sql, postgres=bool(self.database_url))
            try:
                conn.execute(self._sql(_VIEW))
            except Exception:
                # A view is a convenience; the tables are what matter.
                pass
        if not self.database_url:
            _PREPARED_FILES[str(self.db_path)] = self._file_identity()

    def _migrate(self, conn) -> None:
        """Add columns introduced after a database was created."""
        added = {"resume_check": "TEXT", "work_mode": "TEXT", "employment_type": "TEXT"}
        if self.database_url:
            for column, kind in added.items():
                conn.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {column} {kind}")
            conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS candidate_key TEXT")
            # A view's columns cannot be changed in place on Postgres.
            conn.execute("DROP VIEW IF EXISTS job_overview")
            return
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for column, kind in added.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
        # Runs are keyed by candidate identity (email), not by display name.
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "candidate_key" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN candidate_key TEXT")
        # The overview view predates the column; rebuild it to include it.
        conn.execute("DROP VIEW IF EXISTS job_overview")

    def migrations(self) -> List[Dict[str, Any]]:
        """Applied/pending schema migrations for diagnostics."""
        with self._connect() as conn:
            return migration_status(conn, self._sql)

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
        profile = self._profile_snapshot()
        candidate_id = self._candidate_id(profile)

        with self._connect() as conn:
            self._store_profile(conn, now, profile, candidate_id)
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
                    1 if job.is_remote else 0, job.work_mode, job.job_type, job.source,
                    job.date_posted, job.discovered_at, job.salary_min, job.salary_max,
                    job.salary_currency if (job.salary_min or job.salary_max) else None,
                    job.job_url, record.apply_url or route.url, route.channel,
                    1 if route.automatable else 0, route.reason, job.company_website,
                    job.description, status, record.fit_score, record.tailored_resume,
                    record.resume_check, record.notes, now,
                ))
                self._store_source_listing(conn, record, now)
                self._store_match(conn, record, candidate_id, now)
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
                    stats["evaluations"] += self._store_evaluation(conn, record, candidate_id, now)
                if record.application:
                    stats["applications"] += self._store_application(conn, record, candidate_id, now)

            self._backfill_from_csv(conn, set(records) | set(known), now, stats, outputs_dir, candidate_id)
            stats["resumes"] = self._store_resumes(conn, outputs_dir, now, candidate_id)
            self._store_search_parameters(conn, now, candidate_id, profile)
        return stats

    @staticmethod
    def _hash_id(*parts: Any, prefix: str = "") -> str:
        import hashlib

        token = "|".join("" if part is None else str(part) for part in parts)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}{digest}" if prefix else digest

    @staticmethod
    def _as_text(value: Any) -> Optional[str]:
        """Lists are stored as one readable line, not as JSON, for a SQL client."""
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return "; ".join(str(item) for item in value) or None
        return str(value)

    def _profile_snapshot(self) -> Dict[str, Any]:
        """Read the sealed profile once for candidate-owned normalized rows."""
        import json as json_module

        try:
            data = json_module.loads(settings.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _candidate_id(self, profile: Dict[str, Any]) -> str:
        contact = profile.get("contact") or {}
        email = (contact.get("email") or "").casefold()
        profile_hash = profile.get("profile_hash") or ""
        return email or profile_hash or "local-candidate"

    def _split_model(self, scored_by: Optional[str]) -> Dict[str, Optional[str]]:
        if not scored_by:
            return {"provider": None, "name": None}
        provider, _, name = scored_by.partition(":")
        return {"provider": provider or None, "name": name or None}

    def _store_source_listing(self, conn, record: JobRecord, now: str) -> None:
        import json as json_module

        job = record.job
        source_job_id = job.id
        listing_id = self._hash_id(job.source, source_job_id or job.job_url, prefix="src_")
        raw_payload = json_module.dumps(job.model_dump(), default=str)
        conn.execute(self._sql("""
            INSERT INTO job_source_listings (id, job_id, source, source_job_id, source_url, apply_url,
                                             raw_payload, first_seen_at, last_seen_at, source_posted_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (id) DO UPDATE SET
                job_id = excluded.job_id, source_url = excluded.source_url, apply_url = excluded.apply_url,
                raw_payload = excluded.raw_payload, last_seen_at = excluded.last_seen_at,
                source_posted_at = excluded.source_posted_at, is_active = 1
        """), (
            listing_id, job.id, job.source, source_job_id, job.job_url,
            record.apply_url or job.apply_url, raw_payload, job.discovered_at or now,
            now, job.date_posted,
        ))

    def _store_source_listing_from_csv(self, conn, row: Dict[str, str], now: str) -> None:
        import json as json_module

        job_id = row["Job ID"]
        source = row.get("Source") or "csv"
        source_job_id = job_id
        listing_id = self._hash_id(source, source_job_id, prefix="src_")
        conn.execute(self._sql("""
            INSERT INTO job_source_listings (id, job_id, source, source_job_id, source_url, apply_url,
                                             raw_payload, first_seen_at, last_seen_at, source_posted_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (id) DO UPDATE SET
                job_id = excluded.job_id, source_url = excluded.source_url, apply_url = excluded.apply_url,
                raw_payload = excluded.raw_payload, last_seen_at = excluded.last_seen_at,
                source_posted_at = excluded.source_posted_at, is_active = 1
        """), (
            listing_id, job_id, source, source_job_id, row.get("Job URL") or None,
            row.get("Apply URL") or None, json_module.dumps(row, default=str),
            row.get("Date Found") or now, now, row.get("Posted") or None,
        ))

    def _store_match(self, conn, record: JobRecord, candidate_id: str, now: str) -> None:
        evaluation = record.evaluation or {}
        scoring_version = evaluation.get("scoring_version") or "artifact-v1"
        profile_hash = evaluation.get("profile_hash") or self._current_profile_hash(conn)
        match_id = self._hash_id(candidate_id, record.id, scoring_version, profile_hash, prefix="match_")
        conn.execute(self._sql("""
            INSERT INTO job_matches (match_id, candidate_id, job_id, state, fit_score, technical_score,
                                     seniority_score, semantic_score, matching_skills, missing_skills,
                                     scoring_version, prompt_hash, model, profile_hash, tailored_resume,
                                     resume_check, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (match_id) DO UPDATE SET
                state = excluded.state, fit_score = COALESCE(excluded.fit_score, job_matches.fit_score),
                technical_score = COALESCE(excluded.technical_score, job_matches.technical_score),
                seniority_score = COALESCE(excluded.seniority_score, job_matches.seniority_score),
                semantic_score = COALESCE(excluded.semantic_score, job_matches.semantic_score),
                matching_skills = COALESCE(excluded.matching_skills, job_matches.matching_skills),
                missing_skills = COALESCE(excluded.missing_skills, job_matches.missing_skills),
                tailored_resume = COALESCE(excluded.tailored_resume, job_matches.tailored_resume),
                resume_check = COALESCE(excluded.resume_check, job_matches.resume_check),
                notes = COALESCE(excluded.notes, job_matches.notes), updated_at = excluded.updated_at
        """), (
            match_id, candidate_id, record.id, record.status, record.fit_score,
            evaluation.get("technical_score"), evaluation.get("seniority_score"),
            evaluation.get("embedding_similarity"), self._as_text(evaluation.get("matching_skills")),
            self._as_text(evaluation.get("missing_skills")), scoring_version,
            evaluation.get("prompt_hash"), evaluation.get("scored_by"), profile_hash,
            record.tailored_resume, record.resume_check, record.notes, now, now,
        ))

    def _store_match_from_csv(self, conn, row: Dict[str, str], candidate_id: str, now: str) -> None:
        score = row.get("Fit Score") or ""
        fit_score = float(score) if score else None
        state = row.get("Status") or "found"
        match_id = self._hash_id(candidate_id, row["Job ID"], "csv-v1", None, prefix="match_")
        conn.execute(self._sql("""
            INSERT INTO job_matches (match_id, candidate_id, job_id, state, fit_score, scoring_version,
                                     tailored_resume, resume_check, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (match_id) DO UPDATE SET
                state = excluded.state, fit_score = COALESCE(excluded.fit_score, job_matches.fit_score),
                tailored_resume = COALESCE(excluded.tailored_resume, job_matches.tailored_resume),
                resume_check = COALESCE(excluded.resume_check, job_matches.resume_check),
                notes = COALESCE(excluded.notes, job_matches.notes), updated_at = excluded.updated_at
        """), (
            match_id, candidate_id, row["Job ID"], state, fit_score, "csv-v1",
            row.get("Tailored Resume") or None, row.get("Resume Check") or None,
            row.get("Notes") or None, now, now,
        ))

    def _current_profile_hash(self, conn) -> Optional[str]:
        try:
            row = conn.execute(self._sql("SELECT profile_hash FROM candidate_profile WHERE id = 1")).fetchone()
        except Exception:
            return None
        return dict(row).get("profile_hash") if row else None

    def _store_evaluation(self, conn, record: JobRecord, candidate_id: str, now: str) -> int:
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
        model = self._split_model(evaluation.get("scored_by"))
        scoring_version = evaluation.get("scoring_version") or "artifact-v1"
        created_at = evaluation.get("evaluated_at") or now
        evaluation_id = self._hash_id(
            candidate_id, record.id, evaluation.get("profile_hash") or self._current_profile_hash(conn),
            scoring_version, evaluation.get("prompt_hash"), evaluation.get("scored_by"), created_at,
            prefix="eval_",
        )
        conn.execute(self._sql("""
            INSERT INTO job_evaluation_history (evaluation_id, candidate_id, job_id, profile_hash,
                                                embedding_similarity, technical_score, seniority_score,
                                                fit_score, model_provider, model_name, model_version,
                                                prompt_hash, scoring_version, threshold, passed,
                                                reasoning, matching_skills, missing_skills, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (evaluation_id) DO NOTHING
        """), (
            evaluation_id, candidate_id, record.id,
            evaluation.get("profile_hash") or self._current_profile_hash(conn),
            evaluation.get("embedding_similarity"), evaluation.get("technical_score"),
            evaluation.get("seniority_score"), evaluation.get("fit_score"), model["provider"],
            model["name"], evaluation.get("model_version"), evaluation.get("prompt_hash"),
            scoring_version, evaluation.get("threshold_used"),
            1 if evaluation.get("passed_threshold") else 0, evaluation.get("reasoning"),
            self._as_text(evaluation.get("matching_skills")), self._as_text(evaluation.get("missing_skills")),
            created_at,
        ))
        return 1

    def _store_application(self, conn, record: JobRecord, candidate_id: str, now: str) -> int:
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
        self._store_application_current(conn, candidate_id, record.id, status, outcome, now)
        return 1

    def _store_application_current(self, conn, candidate_id: str, job_id: str, status: str,
                                   outcome: Dict[str, Any], now: str) -> str:
        application_id = self._hash_id(candidate_id, job_id, prefix="app_")
        previous = conn.execute(self._sql(
            "SELECT current_status FROM applications WHERE application_id = ?"), (application_id,)).fetchone()
        previous_status = dict(previous)["current_status"] if previous else None
        finished_at = outcome.get("finished_at") or now
        submitted_at = finished_at if status == "applied" else None
        resume_used = Path(outcome["pdf_path"]).name if outcome.get("pdf_path") else outcome.get("resume_used")
        conn.execute(self._sql("""
            INSERT INTO applications (application_id, candidate_id, job_id, current_status, channel, apply_url,
                                      resume_used, submitted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (application_id) DO UPDATE SET
                current_status = excluded.current_status, channel = excluded.channel,
                apply_url = excluded.apply_url, resume_used = excluded.resume_used,
                submitted_at = COALESCE(excluded.submitted_at, applications.submitted_at),
                updated_at = excluded.updated_at
        """), (
            application_id, candidate_id, job_id, status, outcome.get("channel"),
            outcome.get("apply_url"), resume_used, submitted_at, now, finished_at,
        ))
        event_type = "submitted" if status == "applied" else status or "updated"
        self._store_application_event(conn, application_id, event_type, previous_status, status, "sync", outcome,
                                      finished_at)
        return application_id

    def _store_application_event(self, conn, application_id: str, event_type: str, from_status: Optional[str],
                                 to_status: Optional[str], source: str, metadata: Dict[str, Any],
                                 created_at: str) -> None:
        import json as json_module

        event_id = self._hash_id(application_id, event_type, from_status, to_status, created_at, prefix="appevt_")
        conn.execute(self._sql("""
            INSERT INTO application_events (event_id, application_id, event_type, from_status, to_status,
                                            source, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_id) DO NOTHING
        """), (
            event_id, application_id, event_type, from_status, to_status,
            source, json_module.dumps(metadata, default=str), created_at,
        ))

    def _backfill_application(self, conn, row: Dict[str, str], candidate_id: str, now: str) -> int:
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
        self._store_application_current(conn, candidate_id, row["Job ID"], status, {
            "channel": (row.get("Apply Method") or "").replace(" ", "_") or None,
            "apply_url": row.get("Apply URL") or None,
            "resume_used": row.get("Tailored Resume") or None,
            "error": row.get("Notes") or None,
        }, now)
        return 1 if cursor.rowcount else 0

    def _store_profile(self, conn, now: str, data: Optional[Dict[str, Any]] = None,
                       candidate_id: Optional[str] = None) -> None:
        """The sealed candidate profile the run worked from, as one row."""
        data = data if data is not None else self._profile_snapshot()
        candidate_id = candidate_id or self._candidate_id(data)
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
        user_id = (contact.get("email") or candidate_id or "local-user").casefold()
        conn.execute(self._sql("""
            INSERT INTO users (user_id, email, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                email = excluded.email, full_name = excluded.full_name, updated_at = excluded.updated_at
        """), (user_id, contact.get("email"), contact.get("full_name"), now, now))
        conn.execute(self._sql("""
            INSERT INTO candidate_profiles (candidate_id, user_id, profile_hash, full_name, email, phone,
                                            location, years_of_experience, current_country,
                                            authorized_countries, requires_sponsorship, remote_worldwide,
                                            skills, source_resume, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id) DO UPDATE SET
                profile_hash = excluded.profile_hash, full_name = excluded.full_name, email = excluded.email,
                phone = excluded.phone, location = excluded.location,
                years_of_experience = excluded.years_of_experience,
                current_country = excluded.current_country,
                authorized_countries = excluded.authorized_countries,
                requires_sponsorship = excluded.requires_sponsorship,
                remote_worldwide = excluded.remote_worldwide, skills = excluded.skills,
                source_resume = excluded.source_resume, updated_at = excluded.updated_at
        """), (
            candidate_id, user_id, data.get("profile_hash"), contact.get("full_name"), contact.get("email"),
            contact.get("phone"), contact.get("location"), data.get("years_of_experience"),
            auth.get("current_country"), self._as_text(auth.get("authorized_countries")),
            None if auth.get("requires_sponsorship") is None else int(bool(auth["requires_sponsorship"])),
            None if auth.get("remote_worldwide") is None else int(bool(auth["remote_worldwide"])),
            self._as_text(flat), data.get("source_document"), now, now,
        ))

    def _store_search_parameters(self, conn, now: str, candidate_id: Optional[str] = None,
                                 profile: Optional[Dict[str, Any]] = None) -> None:
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
        if candidate_id:
            profile = profile or {}
            conn.execute(self._sql("""
                INSERT INTO candidate_preferences (candidate_id, desired_salary, desired_salary_max,
                                                   salary_currency, target_roles, locations, onsite_countries,
                                                   work_modes, remote_only, hours_old, job_boards,
                                                   country_indeed, min_salary, max_results_per_board,
                                                   find_contacts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (candidate_id) DO UPDATE SET
                    desired_salary = excluded.desired_salary,
                    desired_salary_max = excluded.desired_salary_max,
                    salary_currency = excluded.salary_currency,
                    target_roles = excluded.target_roles, locations = excluded.locations,
                    onsite_countries = excluded.onsite_countries, work_modes = excluded.work_modes,
                    remote_only = excluded.remote_only, hours_old = excluded.hours_old,
                    job_boards = excluded.job_boards, country_indeed = excluded.country_indeed,
                    min_salary = excluded.min_salary,
                    max_results_per_board = excluded.max_results_per_board,
                    find_contacts = excluded.find_contacts, updated_at = excluded.updated_at
            """), (
                candidate_id, profile.get("desired_salary"), profile.get("desired_salary_max"),
                profile.get("salary_currency") or params.salary_currency, self._as_text(params.target_domains),
                self._as_text(params.locations), self._as_text(params.onsite_countries),
                self._as_text(params.selected_work_modes), int(bool(params.is_remote)), params.hours_old,
                self._as_text(params.job_boards), params.country_indeed, params.min_salary,
                params.max_results_per_board, int(bool(params.find_contacts)), now,
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

    # --- Run history ----------------------------------------------------------

    _RUN_JSON = ("phases", "results", "warnings", "totals")

    def save_run(self, run: Dict[str, Any]) -> None:
        """Insert or update one run; called as it starts, after each phase, and when it ends."""
        import json as json_module

        values = [run.get(key) for key in ("run_id", "started_at", "finished_at", "status", "candidate_name",
                                          "candidate_key", "profile_hash", "resume_file")]
        values.append(None if run.get("dry_run") is None else int(bool(run["dry_run"])))
        values += [run.get("tailoring_mode"), run.get("started_from")]
        values += [json_module.dumps(run.get(key), default=str) if run.get(key) is not None else None
                   for key in self._RUN_JSON]
        with self._connect() as conn:
            conn.execute(self._sql("""
                INSERT INTO runs (run_id, started_at, finished_at, status, candidate_name, candidate_key,
                                  profile_hash, resume_file, dry_run, tailoring_mode, started_from, phases,
                                  results, warnings, totals)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    started_at = excluded.started_at, finished_at = excluded.finished_at,
                    status = excluded.status, candidate_name = excluded.candidate_name,
                    candidate_key = excluded.candidate_key,
                    profile_hash = excluded.profile_hash, resume_file = excluded.resume_file,
                    dry_run = excluded.dry_run, tailoring_mode = excluded.tailoring_mode,
                    started_from = excluded.started_from, phases = excluded.phases,
                    results = excluded.results, warnings = excluded.warnings, totals = excluded.totals
            """), values)

    def link_run_jobs(self, run_id: str, job_ids: List[str]) -> None:
        """Remember which jobs a run found, so a past run's shortlist can be reopened."""
        ids = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
        if not ids:
            return
        with self._connect() as conn:
            for job_id in ids:
                conn.execute(self._sql(
                    "INSERT INTO run_jobs (run_id, job_id) VALUES (?, ?) ON CONFLICT (run_id, job_id) DO NOTHING"),
                    (run_id, job_id))

    def list_runs(self, limit: int = 200, candidate: Optional[str] = None) -> List[Dict[str, Any]]:
        """Newest first. Each run carries its job count and decoded JSON fields."""
        import json as json_module

        query = ("SELECT r.*, (SELECT COUNT(*) FROM run_jobs j WHERE j.run_id = r.run_id) AS job_count "
                 "FROM runs r")
        params: List[Any] = []
        if candidate:
            query += " WHERE COALESCE(r.candidate_key, LOWER(r.candidate_name)) = ?"
            params.append(candidate)
        query += " ORDER BY r.started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(self._sql(query), params).fetchall()]
        for row in rows:
            row["candidate_key"] = row.get("candidate_key") or (row.get("candidate_name") or "").casefold() or None
            row["dry_run"] = None if row.get("dry_run") is None else bool(row["dry_run"])
            for key in self._RUN_JSON:
                try:
                    row[key] = json_module.loads(row[key]) if row.get(key) else None
                except ValueError:
                    row[key] = None
        return rows

    def close_orphaned_runs(self, except_run_id: Optional[str] = None, older_than_seconds: float = 30.0) -> int:
        """Mark runs still recorded as "running" as interrupted.

        A run only stays "running" if its process died (the window closed, the
        machine slept, it was killed), because a live run saves its end. The
        caller must know no run is active: the pipeline lock is held, or nothing
        is running. Returns how many were closed.

        `older_than_seconds` guards a race the "is anything running?" check cannot: a run
        may start between that check and this UPDATE, and its brand-new record would be
        marked dead. A record younger than the grace period is never an orphan.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._connect() as conn:
            if except_run_id:
                cursor = conn.execute(self._sql(
                    "UPDATE runs SET status = 'interrupted' WHERE status = 'running' AND run_id != ? "
                    "AND started_at < ?"), (except_run_id, cutoff))
            else:
                cursor = conn.execute(self._sql(
                    "UPDATE runs SET status = 'interrupted' WHERE status = 'running' AND started_at < ?"), (cutoff,))
            return cursor.rowcount or 0

    def run_job_ids(self, run_id: str) -> List[str]:
        with self._connect() as conn:
            return [row["job_id"] for row in conn.execute(
                self._sql("SELECT job_id FROM run_jobs WHERE run_id = ?"), (run_id,)).fetchall()]

    def _store_resumes(self, conn, outputs_dir: Optional[Path], now: str, candidate_id: str) -> int:
        """Keep legacy PDF bytes and normalized file metadata beside each job."""
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
                    None, None, entry.get("tailored_at"), None, None, None, None, None, None, 0, None, None, None,
                    "tailored", entry.get("score"), pdf.name, entry.get("validation_summary"), None, now,
                ))
                known_jobs.add(job_id)
            data = pdf.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            artifact_id = self._hash_id(candidate_id, job_id, digest, prefix="artifact_")
            conn.execute(self._sql("""
                INSERT INTO resume_artifacts (artifact_id, candidate_id, job_id, file_name, file_path,
                                              sha256, size_bytes, resume_check, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    file_path = excluded.file_path, size_bytes = excluded.size_bytes,
                    resume_check = excluded.resume_check
            """), (
                artifact_id, candidate_id, job_id, pdf.name, str(pdf.resolve()), digest,
                len(data), checks.get(job_id), now,
            ))
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
                           outputs_dir: Optional[Path], candidate_id: str) -> None:
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

        for row in rows:
            job_id = row["Job ID"]
            score = row.get("Fit Score") or ""
            auto = (row.get("Auto-apply Possible") or "").strip().lower().startswith("yes")
            conn.execute(self._sql(_UPSERT_JOB), (
                job_id, None, row.get("Title") or "", row.get("Company") or "", row.get("Location"),
                1 if (row.get("Remote") == "Yes") else 0,
                "remote" if row.get("Remote") == "Yes" else None, None,
                row.get("Source"), row.get("Posted") or None, row.get("Date Found") or None,
                float(row["Salary Min"]) if row.get("Salary Min") else None,
                float(row["Salary Max"]) if row.get("Salary Max") else None,
                row.get("Currency") or None, row.get("Job URL"), row.get("Apply URL") or None,
                (row.get("Apply Method") or "").replace(" ", "_") or None, 1 if auto else 0,
                row.get("Auto-apply Possible"), row.get("Company Website") or None, None,
                row.get("Status") or "found", float(score) if score else None,
                row.get("Tailored Resume") or None, row.get("Resume Check") or None,
                row.get("Notes") or None, now,
            ))
            self._store_source_listing_from_csv(conn, row, now)
            self._store_match_from_csv(conn, row, candidate_id, now)
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

        # An outcome recorded by an earlier run survives only in the sheet, so
        # every row's outcome is backfilled after CSV-only jobs have been inserted.
        for row in all_rows:
            stats["applications"] += self._backfill_application(conn, row, candidate_id, now)

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

    def evaluated_jobs(self, *, qualified_only: bool = True, limit: Optional[int] = None,
                       candidate: Optional[str] = None) -> List[Any]:
        """Reconstruct Phase 3 job+evaluation records from normalized DB state."""
        from job_agent.config.schema import EvaluatedJob, EvaluationScore, JobPosting

        where = ["m.fit_score IS NOT NULL"]
        args: List[Any] = []
        if qualified_only:
            where.append("m.fit_score >= ?")
            args.append(settings.min_match_score)
        if candidate:
            where.append("m.candidate_id = ?")
            args.append(candidate)
        clause = " AND ".join(where)
        query = f"""
            SELECT j.job_id, j.title, j.company, j.location, j.job_url, j.description,
                   j.date_posted, j.is_remote, j.work_mode, j.salary_min, j.salary_max,
                   j.salary_currency, j.employment_type, j.source, j.discovered_at,
                   j.apply_url, j.company_website,
                   m.fit_score AS match_fit_score, m.technical_score AS match_technical_score,
                   m.seniority_score AS match_seniority_score, m.semantic_score AS match_semantic_score,
                   m.matching_skills AS match_matching_skills, m.missing_skills AS match_missing_skills,
                   m.model AS match_model, m.updated_at AS match_updated_at,
                   e.embedding_similarity, e.technical_score, e.seniority_score,
                   e.fit_score, e.threshold, e.passed, e.reasoning, e.matching_skills,
                   e.missing_skills, e.model_provider, e.model_name, e.created_at AS evaluated_at
            FROM job_matches m
            JOIN jobs j ON j.job_id = m.job_id
            LEFT JOIN job_evaluation_history e
              ON e.evaluation_id = (
                  SELECT x.evaluation_id
                  FROM job_evaluation_history x
                  WHERE x.candidate_id = m.candidate_id AND x.job_id = m.job_id
                  ORDER BY x.created_at DESC
                  LIMIT 1
              )
            WHERE {clause}
            ORDER BY m.fit_score DESC, m.updated_at DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            args.append(int(limit))

        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(self._sql(query), tuple(args)).fetchall()]

        records = []
        for row in rows:
            try:
                job = JobPosting(
                    id=row["job_id"],
                    title=row["title"],
                    company=row["company"],
                    location=row.get("location") or "Remote",
                    job_url=row.get("job_url") or row.get("apply_url") or "https://example.invalid/job",
                    description=row.get("description") or "",
                    date_posted=row.get("date_posted"),
                    is_remote=bool(row.get("is_remote")),
                    work_mode=row.get("work_mode"),
                    salary_min=row.get("salary_min"),
                    salary_max=row.get("salary_max"),
                    salary_currency=row.get("salary_currency"),
                    job_type=row.get("employment_type"),
                    source=row.get("source") or "database",
                    discovered_at=row.get("discovered_at") or row.get("match_updated_at"),
                    apply_url=row.get("apply_url"),
                    company_website=row.get("company_website"),
                )
                fit_score = row.get("fit_score") if row.get("fit_score") is not None else row.get("match_fit_score")
                threshold = row.get("threshold") if row.get("threshold") is not None else settings.min_match_score
                scored_by = self._join_model(row.get("model_provider"), row.get("model_name")) or row.get("match_model") or "database"
                evaluation = EvaluationScore(
                    embedding_similarity=row.get("embedding_similarity")
                    if row.get("embedding_similarity") is not None else (row.get("match_semantic_score") or 0.0),
                    fit_score=fit_score,
                    technical_score=row.get("technical_score")
                    if row.get("technical_score") is not None else (row.get("match_technical_score") or 0.0),
                    seniority_score=row.get("seniority_score")
                    if row.get("seniority_score") is not None else (row.get("match_seniority_score") or 0.0),
                    threshold_used=threshold,
                    passed_threshold=float(fit_score) >= float(threshold),
                    reasoning=row.get("reasoning") or "Loaded from normalized job match state.",
                    matching_skills=self._split_text(row.get("matching_skills") or row.get("match_matching_skills")),
                    missing_skills=self._split_text(row.get("missing_skills") or row.get("match_missing_skills")),
                    scored_by=scored_by,
                    evaluated_at=row.get("evaluated_at") or row.get("match_updated_at"),
                )
                records.append(EvaluatedJob(job=job, evaluation=evaluation))
            except Exception:
                continue
        return records

    def source_jobs(self, *, limit: Optional[int] = None, active_only: bool = True) -> List[Any]:
        """Reconstruct sourced postings from normalized source-listing state."""
        import json as json_module

        from job_agent.config.schema import JobContact, JobPosting

        where = ["l.is_active = 1"] if active_only else []
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT j.job_id, j.title, j.company, j.location, j.job_url, j.description,
                   j.date_posted, j.is_remote, j.work_mode, j.salary_min, j.salary_max,
                   j.salary_currency, j.employment_type, j.source, j.discovered_at,
                   j.apply_url, j.company_website,
                   l.raw_payload, l.source AS listing_source, l.source_url, l.last_seen_at
            FROM jobs j
            LEFT JOIN job_source_listings l
              ON l.id = (
                  SELECT x.id
                  FROM job_source_listings x
                  WHERE x.job_id = j.job_id
                  ORDER BY x.last_seen_at DESC, x.first_seen_at DESC
                  LIMIT 1
              )
            {clause}
            ORDER BY COALESCE(l.last_seen_at, j.updated_at) DESC, j.company, j.title
        """
        args: List[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            args.append(int(limit))

        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(self._sql(query), tuple(args)).fetchall()]
            contact_rows = [dict(row) for row in conn.execute(self._sql(
                "SELECT job_id, email, kind, source, source_url, confidence FROM job_contacts"
            )).fetchall()]

        contacts_by_job: Dict[str, List[Any]] = {}
        for row in contact_rows:
            try:
                contacts_by_job.setdefault(row["job_id"], []).append(JobContact(
                    email=row["email"],
                    kind=row.get("kind") or "general",
                    source=row.get("source") or "job_post",
                    source_url=row.get("source_url"),
                    confidence=row.get("confidence"),
                ))
            except Exception:
                continue

        jobs = []
        for row in rows:
            raw = row.get("raw_payload")
            payload: Dict[str, Any] = {}
            if raw:
                try:
                    loaded = json_module.loads(raw)
                    payload = loaded if isinstance(loaded, dict) else {}
                except (TypeError, ValueError):
                    payload = {}
            if "Job ID" in payload:
                payload = {}
            payload.update({
                "id": row["job_id"],
                "title": payload.get("title") or row["title"],
                "company": payload.get("company") or row["company"],
                "location": payload.get("location") or row.get("location") or "Remote",
                "job_url": payload.get("job_url") or row.get("job_url") or row.get("source_url") or "https://example.invalid/job",
                "description": payload.get("description") or row.get("description") or "",
                "date_posted": payload.get("date_posted") or row.get("date_posted"),
                "is_remote": payload.get("is_remote") if payload.get("is_remote") is not None else bool(row.get("is_remote")),
                "work_mode": payload.get("work_mode") or row.get("work_mode"),
                "salary_min": payload.get("salary_min") if payload.get("salary_min") is not None else row.get("salary_min"),
                "salary_max": payload.get("salary_max") if payload.get("salary_max") is not None else row.get("salary_max"),
                "salary_currency": payload.get("salary_currency") or row.get("salary_currency"),
                "job_type": payload.get("job_type") or row.get("employment_type"),
                "source": payload.get("source") or row.get("source") or row.get("listing_source") or "database",
                "discovered_at": payload.get("discovered_at") or row.get("discovered_at") or row.get("last_seen_at"),
                "apply_url": payload.get("apply_url") or row.get("apply_url"),
                "company_website": payload.get("company_website") or row.get("company_website"),
                "contacts": payload.get("contacts") or [contact.model_dump() for contact in contacts_by_job.get(row["job_id"], [])],
            })
            try:
                jobs.append(JobPosting(**payload))
            except Exception:
                continue
        return jobs

    @staticmethod
    def _split_text(value: Optional[str]) -> List[str]:
        if not value:
            return []
        return [part.strip() for part in str(value).split(";") if part.strip()]

    @staticmethod
    def _join_model(provider: Optional[str], name: Optional[str]) -> Optional[str]:
        if provider and name:
            return f"{provider}:{name}"
        return provider or name

    def application_stats(self, candidate: Optional[str] = None) -> Dict[str, int]:
        """Application counts from normalized current application state."""
        query = "SELECT current_status, COUNT(*) AS n FROM applications"
        args: List[Any] = []
        if candidate:
            query += " WHERE candidate_id = ?"
            args.append(candidate)
        query += " GROUP BY current_status"
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(self._sql(query), tuple(args)).fetchall()]
        by_status = {row["current_status"] or "unknown": int(row["n"]) for row in rows}
        return {
            "submitted": by_status.get("applied", 0),
            "dry_runs": by_status.get("dry_run", 0),
            "manual_apply": by_status.get("manual_apply", 0) + by_status.get("skipped", 0),
            "failed": by_status.get("failed", 0),
            "total": sum(by_status.values()),
            **{f"status_{key}": value for key, value in by_status.items()},
        }

    def tailored_resumes(self, *, candidate: Optional[str] = None,
                         limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Resume artifacts projected into the Phase 5 manifest shape."""
        where = []
        args: List[Any] = []
        if candidate:
            where.append("ra.candidate_id = ?")
            args.append(candidate)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT ra.job_id, j.title, j.company, ra.file_path, ra.sha256, ra.resume_check,
                   ra.created_at, m.fit_score, m.profile_hash, cp.profile_hash AS candidate_profile_hash
            FROM resume_artifacts ra
            JOIN jobs j ON j.job_id = ra.job_id
            LEFT JOIN candidate_profiles cp ON cp.candidate_id = ra.candidate_id
            LEFT JOIN job_matches m
              ON m.match_id = (
                  SELECT x.match_id
                  FROM job_matches x
                  WHERE x.candidate_id = ra.candidate_id AND x.job_id = ra.job_id
                  ORDER BY x.updated_at DESC, x.created_at DESC
                  LIMIT 1
              )
            {clause}
            ORDER BY ra.created_at DESC, j.company, j.title
        """
        if limit is not None:
            query += " LIMIT ?"
            args.append(int(limit))
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(self._sql(query), tuple(args)).fetchall()]

        records = []
        for row in rows:
            records.append({
                "job_id": row["job_id"],
                "title": row.get("title") or "",
                "company": row.get("company") or "",
                "score": row.get("fit_score"),
                "pdf_path": row.get("file_path") or "",
                "profile_hash": row.get("profile_hash") or row.get("candidate_profile_hash"),
                "pdf_sha256": row.get("sha256"),
                "validation_summary": row.get("resume_check"),
                "tailored_at": row.get("created_at"),
            })
        return records

    def stats(self) -> Dict[str, Any]:
        """Headline numbers: totals, jobs by stage, and contact coverage."""
        with self._connect() as conn:
            total = conn.execute(self._sql("SELECT COUNT(*) AS n FROM job_overview")).fetchone()
            by_status = conn.execute(
                self._sql("SELECT status, COUNT(*) AS n FROM job_overview GROUP BY status")
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
            migrations = migration_status(conn, self._sql)

        number = lambda row: int(dict(row)["n"]) if row else 0
        return {
            "backend": self.backend,
            "location": self.location,
            "schema_version": latest_version(),
            "migrations": migrations,
            "jobs": number(total),
            "by_status": {dict(row)["status"]: int(dict(row)["n"]) for row in by_status},
            "by_source": {dict(row)["source"] or "unknown": int(dict(row)["n"]) for row in by_source},
            "jobs_with_email": number(with_email),
            "jobs_with_hiring_email": number(hiring),
            "outreach_drafts": number(drafts),
            "top_companies": {dict(row)["company"]: int(dict(row)["n"]) for row in top},
        }


_UPSERT_JOB = """
    INSERT INTO jobs (job_id, fingerprint, title, company, location, is_remote, work_mode,
                      employment_type, source, date_posted, discovered_at, salary_min, salary_max,
                      salary_currency, job_url, apply_url, apply_method, auto_apply, apply_note,
                      company_website, description, status, fit_score, tailored_resume,
                      resume_check, notes, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (job_id) DO UPDATE SET
        fingerprint = excluded.fingerprint, title = excluded.title, company = excluded.company,
        location = excluded.location, is_remote = excluded.is_remote,
        work_mode = excluded.work_mode, employment_type = excluded.employment_type,
        source = excluded.source, date_posted = excluded.date_posted,
        salary_min = excluded.salary_min, salary_max = excluded.salary_max,
        salary_currency = excluded.salary_currency, job_url = excluded.job_url, apply_url = excluded.apply_url,
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


def save_run(run: Dict[str, Any], job_ids: Optional[List[str]] = None) -> None:
    """Persist a run record, reporting rather than raising if the database is away."""
    from rich.console import Console

    try:
        database = JobsDatabase()
        database.save_run(run)
        if job_ids:
            database.link_run_jobs(run["run_id"], job_ids)
    except Exception as exc:
        Console().print(f"[dim]Run record not saved: {exc}[/dim]")


def close_orphaned_runs(except_run_id: Optional[str] = None, older_than_seconds: float = 30.0) -> int:
    """Close runs whose process died. Only call when no run can be active."""
    try:
        return JobsDatabase().close_orphaned_runs(except_run_id, older_than_seconds)
    except Exception:
        return 0


def list_runs(limit: int = 200, candidate: Optional[str] = None) -> List[Dict[str, Any]]:
    return JobsDatabase().list_runs(limit=limit, candidate=candidate)


def run_job_ids(run_id: str) -> List[str]:
    return JobsDatabase().run_job_ids(run_id)


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
