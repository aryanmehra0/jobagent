"""Delta store for tracking discovered jobs and deduplicating across sweeps.

SQLite-backed record of every posting the agent has ever seen, so that a job is
evaluated, tailored for, and applied to exactly once. It also carries each
posting's lifecycle status, which is what lets `main.py status` report where work
stalled and lets the tracker distinguish "never attempted" from "attempt failed".
"""

from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from job_agent.config.normalize import clean_text
from job_agent.config.schema import JobPosting, job_fingerprint
from job_agent.config.settings import settings

# Lifecycle states a posting moves through. Kept as a tuple rather than a CHECK
# constraint so that adding a state does not require a database migration.
def profile_identity(path) -> Optional[str]:
    """Who a profile file is for (email, else name); None when it cannot be read."""
    try:
        contact = json.loads(Path(path).read_text(encoding="utf-8")).get("contact") or {}
    except (OSError, ValueError):
        return None
    key = str(contact.get("email") or contact.get("full_name") or "").strip().casefold()
    return key or None


VALID_STATUSES = (
    "scraped",
    "evaluated",
    "qualified",
    "evaluated_rejected",
    "prefilter_rejected",
    "tailored",
    "applied",
    "failed",
    "fallback_logged",
    "replied_rejection", "replied_interview", "replied_offer", "replied_other",
    "skipped",
)


class DeltaStore:
    """Persistent tracking store for discovered job listings."""

    def __init__(self, db_path: Optional[Path] = None):
        self.database_url = settings.database_url if db_path is None else None
        self.db_path = Path(db_path) if db_path else (settings.outputs_dir / "delta_store.db")
        if not self.database_url and db_path is None and settings.postgres_required:
            raise RuntimeError("DATABASE_URL is required when JOB_AGENT_ENVIRONMENT is staging/production.")
        if not self.database_url:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        """Open a connection that commits on success and rolls back on failure.

        `sqlite3.Connection` as a context manager handles the transaction but not
        closing the handle, which leaks file descriptors under Windows and keeps the
        database locked; this wrapper closes it.
        """
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

        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _sql(self, statement: str) -> str:
        if not self.database_url:
            return statement
        return statement.replace("?", "%s")

    def _executemany(self, conn: Any, statement: str, records: Sequence[Sequence[Any]]) -> None:
        if not records:
            return
        sql = self._sql(statement)
        if hasattr(conn, "executemany"):
            conn.executemany(sql, records)
            return
        for record in records:
            conn.execute(sql, record)

    def _init_db(self) -> None:
        """Create the schema and apply in-place migrations for older databases."""
        with self._connect() as conn:
            # WAL keeps reads from blocking while a sweep is writing.
            if not self.database_url:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_url TEXT,
                    company TEXT,
                    title TEXT,
                    location TEXT,
                    source TEXT,
                    first_seen_at TEXT,
                    status TEXT DEFAULT 'scraped'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_url ON seen_jobs(job_url)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_status ON seen_jobs(status)")
            conn.execute("""CREATE TABLE IF NOT EXISTS application_attempts (
                candidate_id TEXT NOT NULL, job_id TEXT NOT NULL, status TEXT NOT NULL,
                outcome_json TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, job_id)
            )""")

            # Migration: track when the status last changed, for stalled-run diagnosis.
            existing = self._columns(conn, "seen_jobs")
            if "location" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN location TEXT")
                existing.add("location")
            if "status_updated_at" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN status_updated_at TEXT")
                conn.execute("UPDATE seen_jobs SET status_updated_at = first_seen_at")

            # Migration: the board-independent fingerprint, so a role seen on one
            # board is not treated as new when it appears on another.
            if "fingerprint" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN fingerprint TEXT")
                rows = conn.execute("SELECT job_id, company, title FROM seen_jobs").fetchall()
                self._executemany(
                    conn,
                    "UPDATE seen_jobs SET fingerprint = ? WHERE job_id = ?",
                    [(job_fingerprint(row["company"] or "", row["title"] or ""), row["job_id"]) for row in rows],
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_fingerprint ON seen_jobs(fingerprint)")
            conn.execute("CREATE TABLE IF NOT EXISTS delta_meta (key TEXT PRIMARY KEY, value TEXT)")

            # Migration: a job can be "reopened" for a new candidate's profile. It stays
            # in the table (its history is kept) but is no longer treated as already seen.
            if "reopened" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN reopened INTEGER DEFAULT 0")

            # One row per outreach email drafted, so no address is written to twice
            # about the same role however many sweeps find it.
            conn.execute("""CREATE TABLE IF NOT EXISTS outreach_log (
                recipient TEXT NOT NULL, fingerprint TEXT NOT NULL, job_id TEXT NOT NULL,
                company TEXT, title TEXT, subject TEXT, body TEXT, drafted_at TEXT NOT NULL,
                PRIMARY KEY(recipient, fingerprint)
            )""")

    def _columns(self, conn: Any, table: str) -> set[str]:
        if self.database_url:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = ? AND table_schema = current_schema()
                """.replace("?", "%s"),
                (table,),
            ).fetchall()
            return {row["column_name"] for row in rows}
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    # --- Reads ----------------------------------------------------------------

    def is_seen(self, job_id: str) -> bool:
        """Whether a job ID exists in the store."""
        with self._connect() as conn:
            return conn.execute(self._sql("SELECT 1 FROM seen_jobs WHERE job_id = ?"), (job_id,)).fetchone() is not None

    def get_seen_count(self) -> int:
        """Total number of tracked listings."""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0])

    def statuses(self, job_ids: Sequence[str]) -> Dict[str, str]:
        """Current lifecycle status of each given job ID that is tracked."""
        if not job_ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(self._sql(
                f"SELECT job_id, status FROM seen_jobs WHERE job_id IN ({','.join('?' * len(job_ids))})",
            ), list(job_ids),
            ).fetchall()
        return {row["job_id"]: row["status"] for row in rows}

    def status_counts(self) -> Dict[str, int]:
        """Listing counts grouped by lifecycle status."""
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM seen_jobs GROUP BY status").fetchall()
        return {row[0] or "unknown": int(row[1]) for row in rows}

    def outreach_counts(self) -> Dict[str, int]:
        """How many outreach drafts have been recorded in the no-repeat ledger."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS drafts,
                    COUNT(DISTINCT recipient) AS recipients,
                    COUNT(DISTINCT fingerprint) AS roles
                FROM outreach_log
                """
            ).fetchone()
        return {
            "drafts": int(row["drafts"] or 0),
            "recipients": int(row["recipients"] or 0),
            "roles": int(row["roles"] or 0),
        }

    def filter_unseen(self, jobs: Sequence[JobPosting]) -> List[JobPosting]:
        """Return only the postings that have not been seen in a previous sweep.

        A posting counts as seen when its ID or its processing fingerprint was
        recorded before. The processing fingerprint includes location evidence
        for non-remote jobs, so same-title roles in London and Bengaluru are not
        silently collapsed as the same vacancy.
        """
        if not jobs:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, fingerprint FROM seen_jobs WHERE COALESCE(reopened, 0) = 0").fetchall()
        seen_ids = {row["job_id"] for row in rows}
        seen_prints = {row["fingerprint"] for row in rows if row["fingerprint"]}
        return [job for job in jobs if job.id not in seen_ids and self.processing_fingerprint(job) not in seen_prints]

    @staticmethod
    def processing_fingerprint(job: JobPosting) -> str:
        """Fingerprint used for destructive/skip decisions, stricter than outreach."""
        base = job_fingerprint(job.company, job.title)
        location = clean_text(job.location).casefold()
        remoteish = job.is_remote or location in {"", "remote", "worldwide", "remote worldwide"}
        if remoteish:
            return base
        return f"{base}|loc:{location}"

    # --- Outreach ledger --------------------------------------------------------

    def outreach_record(self, recipient: str, fingerprint: str) -> Optional[dict]:
        """The earlier draft to this address about this role, if any."""
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT * FROM outreach_log WHERE recipient = ? AND fingerprint = ?"
            ), (recipient.lower(), fingerprint)).fetchone()
        return dict(row) if row else None

    def last_outreach_to(self, recipient: str) -> Optional[dict]:
        """The most recent draft to an address about any role."""
        with self._connect() as conn:
            row = conn.execute(self._sql(
                "SELECT * FROM outreach_log WHERE recipient = ? ORDER BY drafted_at DESC LIMIT 1"
            ), (recipient.lower(),)).fetchone()
        return dict(row) if row else None

    def record_outreach(self, recipient: str, job: JobPosting, subject: str, body: str) -> None:
        with self._connect() as conn:
            conn.execute(
                self._insert_ignore("outreach_log", "(recipient, fingerprint)"),
                (recipient.lower(), job.fingerprint(), job.id, job.company, job.title, subject, body,
                 datetime.now(timezone.utc).isoformat()),
            )

    # --- Writes ---------------------------------------------------------------

    def claim_application(self, candidate_id: str, job_id: str) -> bool:
        """Atomically reserve a live attempt; ambiguous attempts require review."""
        with self._connect() as conn:
            cursor = conn.execute(
                self._insert_ignore("application_attempts", "(candidate_id, job_id)",
                                    "VALUES (?, ?, 'started', NULL, ?)"),
                (candidate_id, job_id, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1

    def application_history(self, candidate_id: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            query = "SELECT candidate_id, job_id, status, outcome_json, updated_at FROM application_attempts"
            args = ()
            if candidate_id:
                query += " WHERE candidate_id=?"
                args = (candidate_id,)
            return [dict(row) for row in conn.execute(self._sql(query + " ORDER BY updated_at DESC"), args)]

    def finish_application(self, candidate_id: str, job_id: str, outcome: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE application_attempts SET status=?, outcome_json=?, updated_at=? WHERE candidate_id=? AND job_id=?"),
                (outcome["status"], json.dumps(outcome), datetime.now(timezone.utc).isoformat(), candidate_id, job_id),
            )

    def mark_seen(self, job: JobPosting, status: str = "scraped") -> None:
        """Register a single posting."""
        self.mark_many_seen([job], status=status)

    def mark_many_seen(self, jobs: Sequence[JobPosting], status: str = "scraped") -> None:
        """Register postings in a single transaction, ignoring ones already present."""
        if not jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        records = [
            (job.id, job.job_url, job.company, job.title, job.location, job.source, now, status, now,
             self.processing_fingerprint(job))
            for job in jobs
        ]
        with self._connect() as conn:
            self._executemany(
                conn,
                self._insert_ignore(
                    "seen_jobs", "(job_id)",
                    """
                    (job_id, job_url, company, title, location, source, first_seen_at, status, status_updated_at, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                ),
                records,
            )
            # A reopened job found again is back in play for the new profile.
            self._executemany(
                conn,
                "UPDATE seen_jobs SET reopened = 0, status = ?, status_updated_at = ? "
                "WHERE job_id = ? AND COALESCE(reopened, 0) = 1",
                [(status, now, job.id) for job in jobs],
            )

    def update_status(self, job_id: str, new_status: str) -> None:
        """Advance a posting's lifecycle status.

        An unknown status is rejected rather than written, since a typo would make
        the posting invisible to every status-based query afterwards.
        """
        if new_status not in VALID_STATUSES:
            raise ValueError(
                f"Unknown delta store status {new_status!r}. Valid: {', '.join(VALID_STATUSES)}"
            )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                self._sql("UPDATE seen_jobs SET status = ?, status_updated_at = ?, reopened = 0 WHERE job_id = ?"),
                (new_status, now, job_id),
            )

    # Statuses that mean "judged for the previous candidate". Applied and replied jobs
    # are real history and are never reopened.
    _REOPENABLE = ("scraped", "evaluated", "qualified", "evaluated_rejected", "prefilter_rejected",
                   "tailored", "failed", "skipped")

    def note_profile(self, identity: Optional[str]) -> int:
        """Record whose profile the stored job statuses were produced for.

        The first call just records it. If the profile has since become a
        different candidate's, by whatever route (intake, a replaced file, the
        CLI), the jobs judged for the old one are reopened. Returns how many.
        """
        if not identity:
            return 0
        with self._connect() as conn:
            row = conn.execute(self._sql("SELECT value FROM delta_meta WHERE key = 'profile_identity'")).fetchone()
            conn.execute(self._sql("INSERT INTO delta_meta (key, value) VALUES ('profile_identity', ?) "
                                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value"), (identity,))
        if row is None or row["value"] == identity:
            return 0
        return self.reopen_for_new_profile()

    def reopen_for_new_profile(self) -> int:
        """Let a different candidate's profile re-consider jobs judged for the last one.

        Statuses are global, not per profile: a job rejected for the previous
        candidate would otherwise never be scored for the new one, and a fresh
        sweep would find almost nothing "new". Reopened jobs are treated as unseen
        the next time a sweep finds them, so only those still in the search
        window come back. Returns how many were reopened.
        """
        marks = ",".join("?" for _ in self._REOPENABLE)
        with self._connect() as conn:
            cursor = conn.execute(
                self._sql(f"UPDATE seen_jobs SET reopened = 1 WHERE COALESCE(reopened, 0) = 0 AND status IN ({marks})"),
                self._REOPENABLE,
            )
            return cursor.rowcount or 0

    def reset(self) -> None:
        """Delete every tracked listing.

        Used by `main.py reset --delta` when the user wants previously seen jobs to
        be re-sourced from scratch.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM seen_jobs")

    def _insert_ignore(self, table: str, conflict_target: str, clause: Optional[str] = None) -> str:
        values = clause or "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        if self.database_url:
            return self._sql(f"INSERT INTO {table} {values} ON CONFLICT {conflict_target} DO NOTHING")
        return f"INSERT OR IGNORE INTO {table} {values}"
