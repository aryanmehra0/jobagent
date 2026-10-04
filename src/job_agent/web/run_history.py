"""The run history: what a run was, who it was for, and how each phase ended.

Kept apart from `runner.py` so the runner only executes phases and publishes events, and this
module owns persistence. Every call here is best effort: history bookkeeping must never fail
a run, and the storage layer already swallows its own errors.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from job_agent.config.settings import settings


def profile_identity() -> Dict[str, Any]:
    """Who a run was for. Read from the sealed profile; never raises."""
    try:
        import json

        data = json.loads(settings.profile_path.read_text(encoding="utf-8"))
        contact = data.get("contact") or {}
        from job_agent.sourcing.delta_store import profile_identity

        # The name is for display; the key (email, else name) is what identifies the
        # candidate, and it is the same key the profile switch uses.
        return {"candidate_name": contact.get("full_name") or contact.get("name"),
                "candidate_key": profile_identity(settings.profile_path),
                "profile_hash": data.get("profile_hash")}
    except Exception:
        return {}


def run_totals(results: Dict[str, Any]) -> Dict[str, Any]:
    """The headline numbers of a run, taken from what each phase reported."""
    def pick(phase: str, *keys: str) -> Any:
        summary = (results.get(phase) or {}).get("summary") or {}
        for key in keys:
            if summary.get(key) is not None:
                return summary[key]
        return None

    totals = {
        "jobs_found": pick("source", "latest_matches", "found"),
        "new_jobs": pick("source", "new_jobs"),
        "scored": pick("evaluate", "scored"),
        "qualified": pick("evaluate", "qualified"),
        "missing_descriptions": pick("evaluate", "missing_descriptions"),
        "resumes": pick("tailor", "compiled"),
        "submitted": pick("apply", "submitted"),
        "simulated": pick("apply", "simulated"),
        "logged": pick("track", "logged"),
        "guides": pick("prep", "guides"),
    }
    return {key: value for key, value in totals.items() if value is not None}


def run_job_ids(results: Dict[str, Any]) -> List[str]:
    """IDs of the jobs a run worked on, from whichever phases it ran.

    A run that only scored and tailored (no new search) still has jobs to show:
    the ones it scored and the ones it built resumes for.
    """
    import json

    out = settings.outputs_dir

    def read(name: str) -> List[Any]:
        try:
            data = json.loads((out / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def db_source_ids() -> List[str]:
        try:
            from job_agent.storage.jobs_db import JobsDatabase

            return [job.id for job in JobsDatabase().source_jobs(limit=100_000)]
        except Exception:
            return []

    def db_evaluated_ids() -> List[str]:
        try:
            from job_agent.storage.jobs_db import JobsDatabase

            return [item.job.id for item in JobsDatabase().evaluated_jobs(qualified_only=False)]
        except Exception:
            return []

    def db_tailored_ids() -> List[str]:
        try:
            from job_agent.storage.jobs_db import JobsDatabase

            return [str(item["job_id"]) for item in JobsDatabase().tailored_resumes(limit=100_000)
                    if item.get("job_id")]
        except Exception:
            return []

    def ran(phase: str) -> bool:
        return (results.get(phase) or {}).get("status") in ("ok", "warning")

    ids: List[str] = []
    if ran("source"):
        source = [str(item["id"]) for item in read("latest_jobs.json") if isinstance(item, dict) and item.get("id")]
        ids += source or db_source_ids()
    if ran("evaluate"):
        evaluated = [str(item["job"]["id"]) for item in read("evaluated_jobs.json")
                     if isinstance(item, dict) and isinstance(item.get("job"), dict) and item["job"].get("id")]
        ids += evaluated or db_evaluated_ids()
    if ran("tailor"):
        tailored = [str(item["job_id"]) for item in read("tailored_resumes/manifest.json")
                    if isinstance(item, dict) and item.get("job_id")]
        ids += tailored or db_tailored_ids()
    return list(dict.fromkeys(ids))


class RunHistory:
    """One run's record, saved as it progresses."""

    def __init__(self, run_id: str, report: Dict[str, Any], phases: List[str], options: Dict[str, Any]):
        self.run_id = run_id
        self.options = options
        self.record: Dict[str, Any] = {
            "run_id": run_id, "started_at": report["started_at"], "status": "running",
            "dry_run": report["dry_run"], "phases": list(phases), "results": {},
            "resume_file": options.get("resume"), "tailoring_mode": options.get("tailoring_mode"),
            "started_from": options.get("started_from", "dashboard"), **profile_identity(),
        }

    @staticmethod
    def _save(record: Dict[str, Any], job_ids: Optional[List[str]] = None) -> None:
        from job_agent.storage.jobs_db import save_run

        save_run(record, job_ids)

    def begin(self) -> None:
        """Save the record, then close any other run still marked running.

        The caller holds the pipeline lock, so a different "running" record lost its
        process (a closed window, a killed task). Say so instead of leaving it running
        for ever; there is no grace period because nothing else can be starting.
        """
        from job_agent.storage.jobs_db import close_orphaned_runs

        self._save(self.record)
        close_orphaned_runs(self.run_id, older_than_seconds=0)

    def phase_ended(self, name: str, status: str, started: float, started_at: str,
                    kept: Dict[str, Any]) -> float:
        """Add a finished phase to the history table and to the run record; returns its duration."""
        from job_agent.storage.jobs_db import record_phase

        duration = round(time.perf_counter() - started, 2)
        record_phase(
            name, status, run_id=self.run_id, started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(), duration_seconds=duration,
            summary={key: value for key, value in (kept or {}).items() if key != "_status"},
            started_from=self.options.get("started_from", "dashboard"),
        )
        self.record["results"][name] = {"status": status, "summary": dict(kept), "duration": duration}
        return duration

    def checkpoint(self) -> None:
        """Save progress. Intake may have just sealed a new profile, so identity is re-read."""
        self.record.update(profile_identity())
        self._save(self.record)

    def finish(self, report: Dict[str, Any]) -> None:
        self.record.update(status=report["status"], finished_at=report["finished_at"],
                           warnings=list(report["warnings"]), totals=run_totals(self.record["results"]),
                           **profile_identity())
        self._save(self.record, run_job_ids(self.record["results"]))
