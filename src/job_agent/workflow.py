"""Shared run publication and durable status for the CLI and dashboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from job_agent.config.settings import settings


def write_run_report(report: dict[str, Any]) -> None:
    path = settings.outputs_dir / "run_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def publish_outputs(*, bundle: bool = False) -> dict[str, Any]:
    """Attempt every export independently; preserve the phase's original error."""
    from job_agent.tracking.export import JobsCsvExporter
    from job_agent.storage.jobs_db import sync_jobs_db

    published: dict[str, Any] = {"files": {}, "warnings": []}
    try:
        path = JobsCsvExporter().export()
        published["files"]["master_csv"] = str(path)
        published["files"]["latest_csv"] = str(path.parent / "jobs_latest.csv")
        published["files"]["ready_csv"] = str(path.parent / "applications_ready.csv")
    except Exception as exc:
        published["warnings"].append(f"CSV export failed: {exc}")
    try:
        if sync_jobs_db() is None:
            published["warnings"].append("Database sync failed; CSV files remain available. See the run log.")
    except Exception as exc:
        published["warnings"].append(f"Database sync failed: {exc}")
    if bundle:
        try:
            from job_agent.tracking.supplements import sync_tracker
            sync_tracker()
        except Exception as exc:
            published["warnings"].append(f"Preparation/outcome tracker update failed: {exc}")
        try:
            from job_agent.tracking.bundle import build_application_pack
            path = build_application_pack()
            published["files"]["application_pack"] = str(path)
        except Exception as exc:
            published["warnings"].append(f"PDF pack export failed: {exc}")
    return published
