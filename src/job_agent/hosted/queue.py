"""Small SQLite-backed queue for hosted control-plane smoke deployments.

This is intentionally narrow. It gives a public API somewhere durable to record
requested runs without exposing the local dashboard. Production installations can
replace the implementation with Redis, Postgres, Cloud Tasks, or a managed queue
while keeping the same payload shape.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from job_agent.config.settings import settings

VALID_PHASES = ("intake", "source", "evaluate", "tailor", "apply", "track", "run-pipeline")


@dataclass(frozen=True)
class HostedRun:
    id: int
    user_id: str
    phases: list[str]
    options: Dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    error: Optional[str] = None


class HostedQueue:
    """Durable run requests for the hosted API and worker."""

    def __init__(self, db_path: Optional[Path] = None):
        self.database_url = settings.database_url if db_path is None else None
        self.db_path = Path(db_path or settings.outputs_dir / "hosted_queue.db")
        if not self.database_url:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _pg_connect(self):
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

    def _init_db(self) -> None:
        if self.database_url:
            with self._pg_connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hosted_runs (
                        id BIGSERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        phases_json TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_status ON hosted_runs(status, id)")
            return
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    phases_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_status ON hosted_runs(status, id)")

    def enqueue(self, user_id: str, phases: list[str], options: Dict[str, Any]) -> HostedRun:
        """Add a pending run after validating the safe hosted payload."""
        user = (user_id or "").strip()
        if not user:
            raise ValueError("user_id is required.")
        unknown = [phase for phase in phases if phase not in VALID_PHASES]
        if unknown:
            raise ValueError(f"Unknown phase(s): {', '.join(unknown)}")
        if not phases:
            raise ValueError("At least one phase is required.")
        if "apply" in phases and not options.get("dry_run", True) and not options.get("allow_live_apply", False):
            raise ValueError("Hosted API accepts live apply only when allow_live_apply is explicitly true.")

        now = datetime.now(timezone.utc).isoformat()
        if self.database_url:
            with self._pg_connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO hosted_runs (user_id, phases_json, options_json, status, created_at, updated_at)
                    VALUES (%s, %s, %s, 'pending', %s, %s)
                    RETURNING id
                    """,
                    (user, json.dumps(phases), json.dumps(options), now, now),
                ).fetchone()
                run_id = int(row["id"])
            return self.get(run_id)  # type: ignore[return-value]
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO hosted_runs (user_id, phases_json, options_json, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (user, json.dumps(phases), json.dumps(options), now, now),
            )
            run_id = int(cursor.lastrowid)
        return self.get(run_id)  # type: ignore[return-value]

    def get(self, run_id: int) -> Optional[HostedRun]:
        if self.database_url:
            with self._pg_connect() as conn:
                row = conn.execute("SELECT * FROM hosted_runs WHERE id = %s", (run_id,)).fetchone()
            return self._row_to_run(row) if row else None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM hosted_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def claim_next(self) -> Optional[HostedRun]:
        """Atomically claim the oldest pending run."""
        now = datetime.now(timezone.utc).isoformat()
        if self.database_url:
            with self._pg_connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM hosted_runs
                    WHERE status = 'pending'
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE hosted_runs SET status = 'running', updated_at = %s WHERE id = %s",
                    (now, row["id"]),
                )
                run_id = int(row["id"])
            return self.get(run_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE hosted_runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            ).rowcount
            if not updated:
                return None
        return self.get(int(row["id"]))

    def finish(self, run_id: int, *, ok: bool, error: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self.database_url:
            with self._pg_connect() as conn:
                conn.execute(
                    "UPDATE hosted_runs SET status = %s, error = %s, updated_at = %s WHERE id = %s",
                    ("succeeded" if ok else "failed", error, now, run_id),
                )
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE hosted_runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                ("succeeded" if ok else "failed", error, now, run_id),
            )

    def counts(self) -> Dict[str, int]:
        if self.database_url:
            with self._pg_connect() as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS count FROM hosted_runs GROUP BY status"
                ).fetchall()
            return {row["status"]: int(row["count"]) for row in rows}
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM hosted_runs GROUP BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    @staticmethod
    def _row_to_run(row) -> HostedRun:
        return HostedRun(
            id=int(row["id"]),
            user_id=row["user_id"],
            phases=json.loads(row["phases_json"]),
            options=json.loads(row["options_json"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
        )

    @property
    def backend(self) -> str:
        return "postgres" if self.database_url else "sqlite"
