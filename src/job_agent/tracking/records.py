"""One typed view of everything the pipeline knows about each job.

The jobs CSV and the jobs database both describe the same thing: a posting, what
it scored, whether a resume was built, how it can be applied to, and what
outreach exists. Assembling that once here keeps the two from drifting, so a
column in the sheet and a column in the database always mean the same thing.

Records are built from the artifacts on disk, which are the pipeline's own
output. Nothing here re-derives or infers anything the pipeline did not record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings

# How far a job has travelled through the pipeline. Apply outcomes share a rank
# so the newest attempt shows, except a confirmed "applied", which is final.
STATUS_RANK: Dict[str, int] = {
    "found": 0, "evaluated": 1, "qualified": 2, "tailored": 3,
    "manual_apply": 4, "skipped": 4, "failed": 4, "dry_run": 4, "applied": 5,
    "replied_other": 6, "replied_rejection": 6, "replied_interview": 6, "replied_offer": 6,
}


def promote_status(current: str, new: str) -> str:
    """Keep the furthest stage reached, so re-sourcing cannot reset "applied"."""
    if current == "applied" and not new.startswith("replied_"):
        return current
    return new if STATUS_RANK.get(new, -1) >= STATUS_RANK.get(current, -1) else current


@dataclass
class JobRecord:
    """A posting plus everything later phases recorded about it."""

    job: JobPosting
    status: str = "found"
    fit_score: Optional[float] = None
    tailored_resume: Optional[str] = None
    resume_check: Optional[str] = None
    apply_url: Optional[str] = None
    notes: Optional[str] = None
    outreach: Dict[str, Any] = field(default_factory=dict)
    evaluation: Dict[str, Any] = field(default_factory=dict)
    application: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.job.id


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def collect_records(outputs_dir: Optional[Path] = None) -> Dict[str, JobRecord]:
    """Every job in the current artifacts, keyed by job ID."""
    out = Path(outputs_dir) if outputs_dir else settings.outputs_dir
    records: Dict[str, JobRecord] = {}

    def postings() -> Iterable[dict]:
        # Richest copy last: an evaluated job carries the same posting plus more.
        yield from _read_json(out / "latest_jobs.json", [])
        yield from _read_json(out / "scraped_jobs.json", [])
        for name in ("evaluated_jobs.json", "qualified_jobs.json"):
            for item in _read_json(out / name, []):
                if isinstance(item, dict) and "job" in item:
                    yield item["job"]

    for raw in postings():
        try:
            job = JobPosting.model_validate(raw)
        except Exception:
            continue
        existing = records.get(job.id)
        if existing is None:
            records[job.id] = JobRecord(job=job)
        else:
            existing.job = job

    qualified_ids = {
        item["job"]["id"] for item in _read_json(out / "qualified_jobs.json", [])
        if isinstance(item, dict) and "job" in item
    }
    for item in _read_json(out / "evaluated_jobs.json", []):
        if not (isinstance(item, dict) and "job" in item):
            continue
        record = records.get(item["job"]["id"])
        if record is None:
            continue
        record.evaluation = item.get("evaluation") or {}
        score = record.evaluation.get("fit_score")
        record.fit_score = None if score is None else float(score)
        record.status = promote_status(record.status, "qualified" if record.id in qualified_ids else "evaluated")

    for entry in _read_json(out / "tailored_resumes" / "manifest.json", []):
        record = records.get(entry.get("job_id")) if isinstance(entry, dict) else None
        if record is None:
            continue
        record.tailored_resume = Path(entry.get("pdf_path", "")).name or None
        record.resume_check = entry.get("validation_summary") or (
            "generated from profile (integrity gate)" if entry.get("pdf_path") else None)
        record.status = promote_status(record.status, "tailored")

    results = _read_json(out / "application_results.json", {})
    if isinstance(results, dict):
        for outcome in list(results.get("successful", [])) + list(results.get("failed", [])):
            record = records.get(outcome.get("job_id"))
            if record is None:
                continue
            # "skipped" means the agent cannot apply there itself, not that the
            # job was passed over.
            status = outcome.get("status") or "failed"
            record.status = promote_status(record.status, "manual_apply" if status == "skipped" else status)
            record.notes = outcome.get("error") or record.notes
            record.apply_url = outcome.get("apply_url") or record.apply_url
            record.application = outcome

    from job_agent.tracking.outreach import load_drafts

    for job_id, draft in load_drafts().items():
        record = records.get(job_id)
        if record is not None and isinstance(draft, dict):
            record.outreach = draft

    for job_id, entry in _read_json(out / "manual_applications.json", {}).items():
        record = records.get(job_id)
        if record is not None and entry.get("status") == "applied":
            record.status = "applied"
            record.notes = "Marked as applied by the candidate on " + entry.get("at", "")

    from job_agent.tracking.inbox import latest_replies
    for job_id, entry in latest_replies(_read_json(out / "inbox_events.json", {})).items():
        if job_id in records:
            records[job_id].status = entry["status"]
    return records


def sorted_records(records: Dict[str, JobRecord]) -> List[JobRecord]:
    """Best first: highest fit score, then most recently discovered."""
    return sorted(
        records.values(),
        key=lambda record: (record.fit_score if record.fit_score is not None else -1.0,
                            record.job.discovered_at or ""),
        reverse=True,
    )
