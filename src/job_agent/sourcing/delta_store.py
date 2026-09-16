"""Delta store for tracking discovered jobs and deduplicating across sweeps.

SQLite-backed record of every posting the agent has ever seen, so that a job is
evaluated, tailored for, and applied to exactly once. It also carries each
posting's lifecycle status, which is what lets `main.py status` report where work
stalled and lets the tracker distinguish "never attempted" from "attempt failed".
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings

# Lifecycle states a posting moves through. Kept as a tuple rather than a CHECK
# constraint so that adding a state does not require a database migration.
VALID_STATUSES = (
    "scraped",
    "evaluated",
    "qualified",
    "evaluated_rejected",
    "tailored",
    "applied",
    "failed",
    "fallback_logged",
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

            # Migration: track when the status last changed, for stalled-run diagnosis.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
            if "status_updated_at" not in existing:
                conn.execute("ALTER TABLE seen_jobs ADD COLUMN status_updated_at TEXT")
                conn.execute("UPDATE seen_jobs SET status_updated_at = first_seen_at")

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

    def status_counts(self) -> Dict[str, int]:
        """Listing counts grouped by lifecycle status."""
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM seen_jobs GROUP BY status").fetchall()
        return {row[0] or "unknown": int(row[1]) for row in rows}

    def filter_unseen(self, jobs: Sequence[JobPosting]) -> List[JobPosting]:
        """Return only the postings that have not been seen in a previous sweep."""
        if not jobs:
            return []
        seen = self.get_all_seen_ids()
        return [job for job in jobs if job.id not in seen]

    # --- Writes ---------------------------------------------------------------

    def mark_seen(self, job: JobPosting, status: str = "scraped") -> None:
        """Register a single posting."""
        self.mark_many_seen([job], status=status)

    def mark_many_seen(self, jobs: Sequence[JobPosting], status: str = "scraped") -> None:
        """Register postings in a single transaction, ignoring ones already present."""
        if not jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        records = [
            (job.id, job.job_url, job.company, job.title, job.source, now, status, now)
            for job in jobs
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO seen_jobs (
                    job_id, job_url, company, title, source, first_seen_at, status, status_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
