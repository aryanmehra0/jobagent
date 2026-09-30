"""Run-time and latency report built from the local phase history."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_agent.config.settings import settings
from job_agent.storage.jobs_db import JobsDatabase


SLOW_SECONDS = {
    "source": 300,
    "evaluate": 600,
    "track": 60,
}


def _load_phase_rows(limit: int = 50) -> list[dict[str, Any]]:
    database = JobsDatabase()
    with database._connect() as conn:
        rows = conn.execute(database._sql("""
            SELECT phase, status, duration_seconds, started_at, summary, started_from
            FROM phase_runs
            WHERE duration_seconds IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
        """), (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["summary"] = json.loads(item.get("summary") or "{}")
        except ValueError:
            item["summary"] = {}
        result.append(item)
    return result


def performance_report(outputs_dir: Path | None = None, limit: int = 50) -> dict[str, Any]:
    rows = _load_phase_rows(limit=limit)
    by_phase: dict[str, list[float]] = {}
    for row in rows:
        by_phase.setdefault(str(row["phase"]), []).append(float(row["duration_seconds"] or 0))

    phase_stats = []
    for phase, values in sorted(by_phase.items()):
        latest = next((float(row["duration_seconds"]) for row in rows if row["phase"] == phase), values[0])
        average = sum(values) / len(values)
        phase_stats.append({
            "phase": phase,
            "runs": len(values),
            "latest_seconds": round(latest, 2),
            "average_seconds": round(average, 2),
            "slow": latest >= SLOW_SECONDS.get(phase, 999999),
        })

    recommendations = []
    latest_by_phase = {item["phase"]: item for item in phase_stats}
    if latest_by_phase.get("evaluate", {}).get("slow"):
        recommendations.append("Evaluation is the latency bottleneck; use --limit for daily runs, keep fallback-on-LLM-error, and reserve full reranks for weekly sweeps.")
    if latest_by_phase.get("source", {}).get("slow"):
        recommendations.append("Sourcing is slow; reduce max_results_per_board or run repair on saved jobs before doing another broad search.")
    if latest_by_phase.get("track", {}).get("slow"):
        recommendations.append("Tracking/outreach is slow; review provider cooldowns and keep cold-email drafting local when speed matters.")
    if not recommendations:
        recommendations.append("No slow phase crossed the local thresholds in the recent run history.")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "recent_runs": len(rows),
        "phase_stats": phase_stats,
        "slowest_recent": sorted(
            [
                {
                    "phase": row["phase"],
                    "status": row["status"],
                    "duration_seconds": round(float(row["duration_seconds"] or 0), 2),
                    "started_at": row.get("started_at") or "",
                    "started_from": row.get("started_from") or "",
                }
                for row in rows
            ],
            key=lambda item: item["duration_seconds"],
            reverse=True,
        )[:10],
        "recommendations": recommendations,
    }


def write_performance_report(outputs_dir: Path | None = None) -> Path:
    out = Path(outputs_dir or settings.outputs_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = performance_report(out)
    path = out / "performance_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
