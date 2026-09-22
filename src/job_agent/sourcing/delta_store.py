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
from typing import Dict, Iterator, List, Optional, Sequence, Set

from job_agent.config.schema import JobPosting, job_fingerprint
from job_agent.config.settings import settings

# Lifecycle states a posting moves through. Kept as a tuple rather than a CHECK
# constraint so that adding a state does not require a database migration.
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
        self.db_path = Path(db_path) if db_path else (settings.outputs_dir / "delta_store.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection that commits on success and rolls back on failure.

        `sqlite3.Connection` as a context manager handles the transaction but not
        closing the handle, which leaks file descriptors under Windows and keeps the
        database locked; this wrapper closes it.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create the schema and apply in-place migrations for older databases."""
        with self._connect() as conn:
            # WAL keeps reads from blocking while a sweep is writing.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_url TEXT,
                    company TEXT,
                    title TEXT,
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
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
            if "status_updated_at" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN status_updated_at TEXT")
                conn.execute("UPDATE seen_jobs SET status_updated_at = first_seen_at")

            # Migration: the board-independent fingerprint, so a role seen on one
            # board is not treated as new when it appears on another.
            if "fingerprint" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN fingerprint TEXT")
                rows = conn.execute("SELECT job_id, company, title FROM seen_jobs").fetchall()
                conn.executemany(
                    "UPDATE seen_jobs SET fingerprint = ? WHERE job_id = ?",
                    [(job_fingerprint(row["company"] or "", row["title"] or ""), row["job_id"]) for row in rows],
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_fingerprint ON seen_jobs(fingerprint)")

            # One row per outreach email drafted, so no address is written to twice
            # about the same role however many sweeps find it.
            conn.execute("""CREATE TABLE IF NOT EXISTS outreach_log (
                recipient TEXT NOT NULL, fingerprint TEXT NOT NULL, job_id TEXT NOT NULL,
                company TEXT, title TEXT, subject TEXT, body TEXT, drafted_at TEXT NOT NULL,
                PRIMARY KEY(recipient, fingerprint)
            )""")

    # --- Reads ----------------------------------------------------------------

    def is_seen(self, job_id: str) -> bool:
        """Whether a job ID exists in the store."""
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone() is not None

    def get_all_seen_ids(self) -> Set[str]:
        """Every seen job ID, as an in-memory set for fast filtering."""
        with self._connect() as conn:
            return {row[0] for row in conn.execute("SELECT job_id FROM seen_jobs")}

    def get_seen_count(self) -> int:
        """Total number of tracked listings."""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0])

    def statuses(self, job_ids: Sequence[str]) -> Dict[str, str]:
        """Current lifecycle status of each given job ID that is tracked."""
        if not job_ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT job_id, status FROM seen_jobs WHERE job_id IN ({','.join('?' * len(job_ids))})",
                list(job_ids),
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

        A posting counts as seen when its ID or its company-plus-title
        fingerprint was recorded before, so the same role reposted on another
        board, or under a new URL, is not processed a second time.
        """
        if not jobs:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT job_id, fingerprint FROM seen_jobs").fetchall()
        seen_ids = {row["job_id"] for row in rows}
        seen_prints = {row["fingerprint"] for row in rows if row["fingerprint"]}
        return [job for job in jobs if job.id not in seen_ids and job.fingerprint() not in seen_prints]

    # --- Outreach ledger --------------------------------------------------------

    def outreach_record(self, recipient: str, fingerprint: str) -> Optional[dict]:
        """The earlier draft to this address about this role, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_log WHERE recipient = ? AND fingerprint = ?",
                (recipient.lower(), fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def last_outreach_to(self, recipient: str) -> Optional[dict]:
        """The most recent draft to an address about any role."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_log WHERE recipient = ? ORDER BY drafted_at DESC LIMIT 1",
                (recipient.lower(),),
            ).fetchone()
        return dict(row) if row else None

    def record_outreach(self, recipient: str, job: JobPosting, subject: str, body: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO outreach_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (recipient.lower(), job.fingerprint(), job.id, job.company, job.title, subject, body,
                 datetime.now(timezone.utc).isoformat()),
            )

    # --- Writes ---------------------------------------------------------------

    def claim_application(self, candidate_id: str, job_id: str) -> bool:
        """Atomically reserve a live attempt; ambiguous attempts require review."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO application_attempts VALUES (?, ?, 'started', NULL, ?)",
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
            return [dict(row) for row in conn.execute(query + " ORDER BY updated_at DESC", args)]

    def finish_application(self, candidate_id: str, job_id: str, outcome: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_attempts SET status=?, outcome_json=?, updated_at=? WHERE candidate_id=? AND job_id=?",
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
            (job.id, job.job_url, job.company, job.title, job.source, now, status, now, job.fingerprint())
            for job in jobs
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO seen_jobs (
                    job_id, job_url, company, title, source, first_seen_at, status, status_updated_at, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
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
                "UPDATE seen_jobs SET status = ?, status_updated_at = ? WHERE job_id = ?",
                (new_status, now, job_id),
            )

    def reset(self) -> None:
        """Delete every tracked listing.

        Used by `main.py reset --delta` when the user wants previously seen jobs to
        be re-sourced from scratch.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM seen_jobs")
