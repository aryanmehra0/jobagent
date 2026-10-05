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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from job_agent.config.settings import settings

VALID_PHASES = ("intake", "source", "evaluate", "tailor", "apply", "track", "prep", "run-pipeline")


@dataclass(frozen=True)
class HostedRun:
    id: int
    user_id: str
    phases: list[str]
    options: Dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    idempotency_key: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    claimed_at: Optional[str] = None
    started_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    finished_at: Optional[str] = None


class HostedQueue:
    """Durable run requests for the hosted API and worker."""

    def __init__(self, db_path: Optional[Path] = None):
        self.database_url = settings.database_url if db_path is None else None
        self.db_path = Path(db_path or settings.outputs_dir / "hosted_queue.db")
        if not self.database_url and db_path is None and settings.postgres_required:
            raise RuntimeError("DATABASE_URL is required when JOB_AGENT_ENVIRONMENT is staging/production.")
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
                        idempotency_key TEXT,
                        phases_json TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        claimed_at TEXT,
                        started_at TEXT,
                        heartbeat_at TEXT,
                        finished_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._migrate_columns(conn)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_status ON hosted_runs(status, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_heartbeat ON hosted_runs(status, heartbeat_at)")
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_hosted_runs_user_idempotency
                    ON hosted_runs(user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                    """
                )
            return
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hosted_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    phases_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    claimed_at TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._migrate_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_status ON hosted_runs(status, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hosted_runs_heartbeat ON hosted_runs(status, heartbeat_at)")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_hosted_runs_user_idempotency
                ON hosted_runs(user_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )

    def _migrate_columns(self, conn) -> None:
        columns = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "claimed_at": "TEXT",
            "started_at": "TEXT",
            "heartbeat_at": "TEXT",
            "finished_at": "TEXT",
            "idempotency_key": "TEXT",
        }
        if self.database_url:
            for column, kind in columns.items():
                conn.execute(f"ALTER TABLE hosted_runs ADD COLUMN IF NOT EXISTS {column} {kind}")
            return
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(hosted_runs)").fetchall()}
        for column, kind in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE hosted_runs ADD COLUMN {column} {kind}")

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
        key = (idempotency_key or "").strip()
        if not key:
            return None
        if len(key) > 200:
            raise ValueError("idempotency_key must be 200 characters or fewer.")
        if any(ch in key for ch in "\r\n\t"):
            raise ValueError("idempotency_key must not contain control whitespace.")
        return key

    def _get_by_idempotency_key(self, user_id: str, idempotency_key: str) -> Optional[HostedRun]:
        if self.database_url:
            with self._pg_connect() as conn:
                row = conn.execute(
                    "SELECT * FROM hosted_runs WHERE user_id = %s AND idempotency_key = %s ORDER BY id LIMIT 1",
                    (user_id, idempotency_key),
                ).fetchone()
            return self._row_to_run(row) if row else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE user_id = ? AND idempotency_key = ? ORDER BY id LIMIT 1",
                (user_id, idempotency_key),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def enqueue(
        self,
        user_id: str,
        phases: list[str],
        options: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> HostedRun:
        """Add a pending run after validating the safe hosted payload."""
        user = (user_id or "").strip()
        if not user:
            raise ValueError("user_id is required.")
        normalized_key = self._normalize_idempotency_key(idempotency_key)
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
                    INSERT INTO hosted_runs (
                        user_id, idempotency_key, phases_json, options_json, status, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                    ON CONFLICT (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
                    RETURNING id
                    """,
                    (user, normalized_key, json.dumps(phases), json.dumps(options), now, now),
                ).fetchone()
                if row is None and normalized_key is not None:
                    existing = conn.execute(
                        "SELECT * FROM hosted_runs WHERE user_id = %s AND idempotency_key = %s ORDER BY id LIMIT 1",
                        (user, normalized_key),
                    ).fetchone()
                    return self._row_to_run(existing)
                run_id = int(row["id"])
            return self.get(run_id)  # type: ignore[return-value]
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO hosted_runs (
                    user_id, idempotency_key, phases_json, options_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (user, normalized_key, json.dumps(phases), json.dumps(options), now, now),
            )
            if cursor.rowcount == 0 and normalized_key is not None:
                row = conn.execute(
                    "SELECT * FROM hosted_runs WHERE user_id = ? AND idempotency_key = ? ORDER BY id LIMIT 1",
                    (user, normalized_key),
                ).fetchone()
                return self._row_to_run(row)
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
                    WHERE status IN ('pending', 'retryable')
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    """
                    UPDATE hosted_runs
                    SET status = 'running', attempts = attempts + 1,
                        claimed_at = %s, started_at = COALESCE(started_at, %s),
                        heartbeat_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (now, now, now, now, row["id"]),
                )
                run_id = int(row["id"])
            return self.get(run_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hosted_runs WHERE status IN ('pending', 'retryable') ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE hosted_runs
                SET status = 'running', attempts = attempts + 1,
                    claimed_at = ?, started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retryable')
                """,
                (now, now, now, now, row["id"]),
            ).rowcount
            if not updated:
                return None
        return self.get(int(row["id"]))

    def finish(self, run_id: int, *, ok: bool, error: Optional[str] = None, retryable: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status = "succeeded" if ok else ("retryable" if retryable else "failed")
        if self.database_url:
            with self._pg_connect() as conn:
                conn.execute(
                    """
                    UPDATE hosted_runs
                    SET status = CASE WHEN %s = 'retryable' AND attempts >= max_attempts THEN 'failed' ELSE %s END,
                        error = %s, finished_at = CASE WHEN %s IN ('succeeded', 'failed') THEN %s ELSE finished_at END,
                        heartbeat_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (status, status, error, status, now, now, now, run_id),
                )
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE hosted_runs
                SET status = CASE WHEN ? = 'retryable' AND attempts >= max_attempts THEN 'failed' ELSE ? END,
                    error = ?, finished_at = CASE WHEN ? IN ('succeeded', 'failed') THEN ? ELSE finished_at END,
                    heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, status, error, status, now, now, now, run_id),
            )

    def heartbeat(self, run_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self.database_url:
            with self._pg_connect() as conn:
                conn.execute("UPDATE hosted_runs SET heartbeat_at = %s, updated_at = %s WHERE id = %s AND status = 'running'",
                             (now, now, run_id))
            return
        with self._connect() as conn:
            conn.execute("UPDATE hosted_runs SET heartbeat_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                         (now, now, run_id))

    def recover_stale(self, *, older_than_seconds: float = 300.0) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        if self.database_url:
            with self._pg_connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE hosted_runs
                    SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retryable' END,
                        error = COALESCE(error, 'Worker heartbeat expired.'),
                        updated_at = %s
                    WHERE status = 'running' AND COALESCE(heartbeat_at, claimed_at, updated_at) < %s
                    """,
                    (datetime.now(timezone.utc).isoformat(), cutoff),
                )
                return cursor.rowcount or 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE hosted_runs
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retryable' END,
                    error = COALESCE(error, 'Worker heartbeat expired.'),
                    updated_at = ?
                WHERE status = 'running' AND COALESCE(heartbeat_at, claimed_at, updated_at) < ?
                """,
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            return cursor.rowcount or 0

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

    def list_runs(self, *, status: Optional[str] = None, user_id: Optional[str] = None,
                  limit: int = 50) -> list[HostedRun]:
        bounded = max(1, min(500, int(limit)))
        where = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if user_id:
            where.append("user_id = ?")
            args.append(user_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        if self.database_url:
            with self._pg_connect() as conn:
                query = f"SELECT * FROM hosted_runs{clause} ORDER BY id DESC LIMIT ?".replace("?", "%s")
                rows = conn.execute(query, tuple(args + [bounded])).fetchall()
            return [self._row_to_run(row) for row in rows]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM hosted_runs{clause} ORDER BY id DESC LIMIT ?",
                tuple(args + [bounded]),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

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
            idempotency_key=row["idempotency_key"],
            error=row["error"],
            attempts=int(row["attempts"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            claimed_at=row["claimed_at"],
            started_at=row["started_at"],
            heartbeat_at=row["heartbeat_at"],
            finished_at=row["finished_at"],
        )

    @property
    def backend(self) -> str:
        return "postgres" if self.database_url else "sqlite"
