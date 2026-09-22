"""Local quality score for the current application data and downloads."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_agent.config.settings import settings
from job_agent.intake.validator import load_and_verify_profile
from job_agent.tracking.bundle import validate_application_pack


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _freshness_score(coverage: dict[str, Any]) -> tuple[int, str]:
    checked_at = coverage.get("checked_at")
    if not checked_at:
        return 0, "No source_coverage.json timestamp."
    try:
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0, f"Unreadable source timestamp: {checked_at}"
    age_hours = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() / 3600
    window = float(coverage.get("hours_old") or 48)
    if age_hours <= window:
        return 15, f"Latest sweep is within the {window:g}h search window."
    if age_hours <= window * 2:
        return 8, f"Latest sweep is {age_hours:.1f}h old; refresh soon."
    return 0, f"Latest sweep is {age_hours:.1f}h old; run python main.py daily."


def quality_report(outputs_dir: Path | None = None) -> dict[str, Any]:
    """Score the application workspace on the things that affect applying today."""
    out = Path(outputs_dir or settings.outputs_dir)
    master = _read_rows(out / "jobs_master.csv")
    latest = _read_rows(out / "jobs_latest.csv")
    ready = _read_rows(out / "applications_ready.csv")
    coverage = _read_json(out / "source_coverage.json", {})
    evaluation = _read_json(out / "evaluation_progress.json", {})
    checks: list[dict[str, Any]] = []

    def add(name: str, points: int, maximum: int, detail: str) -> None:
        checks.append({"name": name, "points": points, "max": maximum, "detail": detail})

    try:
        profile, valid = load_and_verify_profile(settings.profile_path)
        add("profile_seal", 15 if valid else 0, 15,
            f"{profile.contact.full_name}; locked facts verified." if valid else "Profile seal failed.")
    except Exception as exc:
        add("profile_seal", 0, 15, f"Profile could not be verified: {exc}")

    points, detail = _freshness_score(coverage if isinstance(coverage, dict) else {})
    add("source_freshness", points, 15, detail)

    scored = int(evaluation.get("scored") or 0) if isinstance(evaluation, dict) else 0
    missing = int(evaluation.get("missing_descriptions") or 0) if isinstance(evaluation, dict) else 0
    failed = int(evaluation.get("failed") or 0) if isinstance(evaluation, dict) else 0
    total_eval = scored + missing + failed
    if total_eval:
        eval_points = round(15 * scored / total_eval)
        detail = f"{scored}/{total_eval} jobs had readable descriptions and were scored."
    else:
        eval_points = 0
        detail = "No evaluation progress recorded."
    if failed:
        eval_points = max(0, eval_points - 3)
        detail += f" {failed} evaluation(s) failed."
    add("evaluation_coverage", eval_points, 15, detail)

    latest_count = len(latest)
    ready_count = len(ready)
    if latest_count:
        readiness_points = min(15, round(15 * ready_count / max(1, min(latest_count, 10))))
        detail = f"{ready_count} ready row(s) from {latest_count} latest-search row(s)."
    else:
        readiness_points = 0
        detail = "No latest-search CSV rows."
    add("application_readiness", readiness_points, 15, detail)

    pack = out / "application_pack.zip"
    try:
        stats = validate_application_pack(pack)
        document_points = 20 if stats["checked_documents"] else 12
        add("download_pack", document_points, 20,
            f"{stats['checked_documents']} document hash(es) verified in application_pack.zip.")
    except Exception as exc:
        add("download_pack", 0, 20, f"Pack is not valid: {exc}")

    contact_ready = [row for row in ready if row.get("HR / Careers Email")]
    contact_points = 10 if contact_ready else (5 if ready else 0)
    add("contact_coverage", contact_points, 10,
        f"{len(contact_ready)}/{ready_count} ready job(s) have a published hiring email.")

    applied_or_replied = [
        row for row in master
        if row.get("Status") == "applied" or (row.get("Status") or "").startswith("replied_")
    ]
    add("outcome_tracking", 10 if applied_or_replied else 5, 10,
        f"{len(applied_or_replied)} job(s) have applied/reply outcome state.")

    score = sum(item["points"] for item in checks)
    maximum = sum(item["max"] for item in checks)
    grade = round(score * 10 / maximum, 1) if maximum else 0.0
    blockers = [item for item in checks if item["points"] < item["max"] * 0.5]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "max_score": maximum,
        "grade_out_of_10": grade,
        "jobs": {"master": len(master), "latest": len(latest), "ready": len(ready)},
        "checks": checks,
        "next_actions": [item["detail"] for item in blockers[:5]],
    }


def write_quality_report(outputs_dir: Path | None = None) -> Path:
    out = Path(outputs_dir or settings.outputs_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = quality_report(out)
    path = out / "quality_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
