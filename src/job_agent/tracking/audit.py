"""Cross-check saved artifacts against the database and downloadable pack."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_agent.config.settings import settings
from job_agent.storage.jobs_db import JobsDatabase, sync_jobs_db
from job_agent.tracking.bundle import validate_application_pack


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _count_table(database: JobsDatabase, table: str) -> int:
    with database._connect() as conn:
        row = conn.execute(database._sql(f"SELECT COUNT(*) AS n FROM {table}")).fetchone()
    return int(dict(row)["n"])


def audit_report(outputs_dir: Path | None = None, *, sync: bool = True) -> dict[str, Any]:
    """Return an auditable state snapshot for the current run outputs."""
    out = Path(outputs_dir or settings.outputs_dir)
    if sync:
        sync_jobs_db(out)
    database = JobsDatabase()
    master = _rows(out / "jobs_master.csv")
    latest = _rows(out / "jobs_latest.csv")
    ready = _rows(out / "applications_ready.csv")
    qualified = _json(out / "qualified_jobs.json", [])
    evaluated = _json(out / "evaluated_jobs.json", [])
    quality = _json(out / "quality_report.json", {})
    performance = _json(out / "performance_report.json", {})
    db_stats = database.stats()
    pack_stats: dict[str, Any]
    try:
        pack_stats = validate_application_pack(out / "application_pack.zip")
    except Exception as exc:
        pack_stats = {"valid": False, "error": str(exc)}

    db_counts = {
        "jobs": db_stats["jobs"],
        "contacts": _count_table(database, "job_contacts"),
        "resumes": _count_table(database, "job_resumes"),
        "evaluations": _count_table(database, "job_evaluations"),
        "applications": _count_table(database, "job_applications"),
        "outreach": _count_table(database, "job_outreach"),
    }
    artifact_counts = {
        "jobs_master_rows": len(master),
        "latest_rows": len(latest),
        "ready_rows": len(ready),
        "qualified_json": len(qualified) if isinstance(qualified, list) else 0,
        "evaluated_json": len(evaluated) if isinstance(evaluated, list) else 0,
        "pack_documents": int(pack_stats.get("checked_documents") or 0),
    }
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("database_matches_master_csv", db_counts["jobs"] == artifact_counts["jobs_master_rows"],
        f"database jobs={db_counts['jobs']}, jobs_master rows={artifact_counts['jobs_master_rows']}")
    add("ready_rows_have_resumes", db_counts["resumes"] >= artifact_counts["ready_rows"],
        f"stored resumes={db_counts['resumes']}, ready rows={artifact_counts['ready_rows']}")
    add("pack_valid", bool(pack_stats.get("valid")),
        f"pack checked documents={pack_stats.get('checked_documents', 0)}")
    add("qualified_have_tailored_docs", artifact_counts["qualified_json"] <= db_counts["resumes"],
        f"qualified jobs={artifact_counts['qualified_json']}, stored resumes={db_counts['resumes']}")
    add("quality_report_present", bool(quality),
        f"quality={quality.get('grade_out_of_10', 'missing')}/10")
    add("performance_report_present", bool(performance),
        f"recent phase runs={performance.get('recent_runs', 'missing')}")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "database": {"backend": db_stats["backend"], "location": db_stats["location"], "counts": db_counts},
        "artifacts": artifact_counts,
        "quality": {"grade_out_of_10": quality.get("grade_out_of_10"), "ready": quality.get("jobs", {}).get("ready")},
        "pack": pack_stats,
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
    }


def write_audit_report(outputs_dir: Path | None = None) -> Path:
    out = Path(outputs_dir or settings.outputs_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = audit_report(out)
    path = out / "audit_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
