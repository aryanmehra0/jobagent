"""Small, explicit database migrations for the jobs store.

This project still supports SQLite for local use and Postgres through
``DATABASE_URL``. These migrations are intentionally plain SQL so the same
bootstrap path works in both modes; they also give operators a visible
``schema_migrations`` table instead of hiding every schema change inside
``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, List


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Any, Callable[[str], str], bool], None]


def _add_column(conn: Any, sql: Callable[[str], str], postgres: bool, table: str, column: str, kind: str) -> None:
    if postgres:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        return
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _migration_001(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    _add_column(conn, sql, postgres, "jobs", "resume_check", "TEXT")
    _add_column(conn, sql, postgres, "runs", "candidate_key", "TEXT")


def _migration_002(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    _add_column(conn, sql, postgres, "jobs", "work_mode", "TEXT")
    _add_column(conn, sql, postgres, "jobs", "employment_type", "TEXT")


def _migration_003(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    # The tables themselves are created from the canonical schema before
    # migrations run. This migration records when a database became compatible
    # with the normalized candidate/job/application model.
    conn.execute(sql("CREATE INDEX IF NOT EXISTS idx_job_matches_lookup ON job_matches(candidate_id, state, fit_score)"))
    conn.execute(sql(
        "CREATE INDEX IF NOT EXISTS idx_applications_status "
        "ON applications(candidate_id, current_status, updated_at)"
    ))
    conn.execute(sql(
        "CREATE INDEX IF NOT EXISTS idx_application_events_timeline "
        "ON application_events(application_id, created_at)"
    ))


def _migration_004(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    """Separate global job lifecycle from candidate/application lifecycle state."""
    _add_column(conn, sql, postgres, "jobs", "is_active", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, sql, postgres, "jobs", "first_seen_at", "TEXT")
    _add_column(conn, sql, postgres, "jobs", "last_seen_at", "TEXT")
    _add_column(conn, sql, postgres, "jobs", "closed_at", "TEXT")
    _add_column(conn, sql, postgres, "jobs", "archived_at", "TEXT")
    conn.execute(sql("UPDATE jobs SET status = 'active' WHERE status NOT IN ('active', 'inactive', 'expired', 'closed', 'archived', 'unknown')"))
    conn.execute(sql("UPDATE jobs SET is_active = CASE WHEN status IN ('closed', 'archived', 'expired', 'inactive') THEN 0 ELSE 1 END WHERE is_active IS NULL OR is_active NOT IN (0, 1)"))
    conn.execute(sql("UPDATE jobs SET first_seen_at = COALESCE(first_seen_at, discovered_at, updated_at)"))
    conn.execute(sql("UPDATE jobs SET last_seen_at = COALESCE(last_seen_at, updated_at, discovered_at)"))
    conn.execute(sql("CREATE INDEX IF NOT EXISTS idx_jobs_lifecycle_seen ON jobs(status, is_active, last_seen_at)"))


def _migration_005(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    """Record durable artifact storage metadata beside local compatibility paths."""
    _add_column(conn, sql, postgres, "resume_artifacts", "storage_backend", "TEXT NOT NULL DEFAULT 'local'")
    _add_column(conn, sql, postgres, "resume_artifacts", "object_key", "TEXT")
    _add_column(conn, sql, postgres, "resume_artifacts", "mime_type", "TEXT")
    _add_column(conn, sql, postgres, "resume_artifacts", "version", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(sql("UPDATE resume_artifacts SET storage_backend = COALESCE(storage_backend, 'local')"))
    conn.execute(sql("UPDATE resume_artifacts SET object_key = COALESCE(object_key, file_path)"))
    conn.execute(sql("UPDATE resume_artifacts SET mime_type = COALESCE(mime_type, 'application/pdf')"))
    conn.execute(sql("UPDATE resume_artifacts SET version = COALESCE(version, 1)"))


def _migration_006(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    """Append-only run events for worker/API/LLM observability."""
    conn.execute(sql("""
        CREATE TABLE IF NOT EXISTS run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT,
            candidate_id TEXT,
            job_id TEXT,
            phase TEXT NOT NULL,
            event_type TEXT NOT NULL,
            success INTEGER,
            latency_ms INTEGER,
            error_code TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    """))
    conn.execute(sql("CREATE INDEX IF NOT EXISTS idx_run_events_run_time ON run_events(run_id, created_at)"))
    conn.execute(sql("CREATE INDEX IF NOT EXISTS idx_run_events_phase_time ON run_events(phase, created_at)"))


def _migration_007(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    """Persist generated interview prep guide metadata in the jobs database."""
    conn.execute(sql("""
        CREATE TABLE IF NOT EXISTS interview_prep_artifacts (
            prep_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            json_path TEXT,
            sha256 TEXT NOT NULL,
            questions INTEGER,
            profile_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """))
    conn.execute(sql(
        "CREATE INDEX IF NOT EXISTS idx_interview_prep_candidate_job "
        "ON interview_prep_artifacts(candidate_id, job_id)"
    ))


def _migration_008(conn: Any, sql: Callable[[str], str], postgres: bool) -> None:
    """Persist generated cover letter metadata in the jobs database."""
    conn.execute(sql("""
        CREATE TABLE IF NOT EXISTS cover_letter_artifacts (
            letter_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidate_profiles(candidate_id) ON DELETE CASCADE,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            validated INTEGER,
            profile_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """))
    conn.execute(sql(
        "CREATE INDEX IF NOT EXISTS idx_cover_letter_candidate_job "
        "ON cover_letter_artifacts(candidate_id, job_id)"
    ))


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "legacy_candidate_columns", _migration_001),
    Migration(2, "canonical_job_work_mode", _migration_002),
    Migration(3, "normalized_candidate_job_state", _migration_003),
    Migration(4, "global_job_lifecycle", _migration_004),
    Migration(5, "artifact_storage_metadata", _migration_005),
    Migration(6, "run_events_observability", _migration_006),
    Migration(7, "interview_prep_artifacts", _migration_007),
    Migration(8, "cover_letter_artifacts", _migration_008),
)


def ensure_migration_table(conn: Any, sql: Callable[[str], str]) -> None:
    conn.execute(sql("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """))


def applied_versions(conn: Any, sql: Callable[[str], str]) -> set[int]:
    ensure_migration_table(conn, sql)
    return {int(row["version"]) for row in conn.execute(sql("SELECT version FROM schema_migrations")).fetchall()}


def migration_status(conn: Any, sql: Callable[[str], str]) -> List[dict[str, Any]]:
    applied = applied_versions(conn, sql)
    return [
        {"version": migration.version, "name": migration.name, "applied": migration.version in applied}
        for migration in MIGRATIONS
    ]


def run_migrations(conn: Any, sql: Callable[[str], str], *, postgres: bool) -> list[Migration]:
    ensure_migration_table(conn, sql)
    applied = applied_versions(conn, sql)
    ran: list[Migration] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(conn, sql, postgres)
        conn.execute(sql(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)"
        ), (migration.version, migration.name))
        ran.append(migration)
    return ran


def latest_version() -> int:
    return max((migration.version for migration in MIGRATIONS), default=0)
