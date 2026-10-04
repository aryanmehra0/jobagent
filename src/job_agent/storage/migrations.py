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


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "legacy_candidate_columns", _migration_001),
    Migration(2, "canonical_job_work_mode", _migration_002),
    Migration(3, "normalized_candidate_job_state", _migration_003),
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
