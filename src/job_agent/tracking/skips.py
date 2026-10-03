"""Jobs the candidate chose to skip, kept on the server so the rest of the pipeline can learn from it.

Skipping used to be a browser-only viewing preference: it hid a row and nothing else knew. That
threw away the one signal the candidate gives for free. A skip is now recorded here and in the
seen-jobs store, so the job is not scored again, not resurfaced by the next search, and its reason
is available to later analysis. It is still not an application outcome: nothing reaches the tracker.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from job_agent.config.normalize import read_json, utc_now_iso
from job_agent.config.settings import settings
from job_agent.runtime import exclusive_run

SKIPS_FILE = "user_skips.json"
MAX_REASON_CHARS = 200


def skips_path(outputs_dir=None):
    return (outputs_dir or settings.outputs_dir) / SKIPS_FILE


def load_skips(outputs_dir=None) -> Dict[str, Dict[str, Any]]:
    data = read_json(skips_path(outputs_dir), {})
    return data if isinstance(data, dict) else {}


def feedback_note(outputs_dir=None, limit: int = 6, max_chars: int = 600) -> str:
    """The candidate's own reasons for skipping jobs, as context for the scoring judge.

    Only skips with a written reason count: "skipped" alone says nothing about why. The most
    recent come first and the text is bounded, because the judge's prompt has a token budget.
    """
    entries = [item for item in load_skips(outputs_dir).values()
               if isinstance(item, dict) and str(item.get("reason") or "").strip()]
    entries.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    lines = []
    for item in entries[:limit]:
        role = " at ".join(part for part in (str(item.get("title") or "").strip(),
                                             str(item.get("company") or "").strip()) if part) or "a role"
        lines.append(f"- {role}: {str(item['reason']).strip()}")
    if not lines:
        return ""
    note = ("Roles the candidate already rejected, with their own reason. Treat a similar role as a weaker "
            "match for the same reason; do not treat this as new facts about the candidate:\n" + "\n".join(lines))
    return note[:max_chars]


@exclusive_run
def mark_skipped(job_id: str, undo: bool = False, reason: str = "") -> Dict[str, Any]:
    """Record (or undo) a skip. Raises ValueError for a job the shortlist does not know."""
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tracking.export import JobsCsvExporter

    rows = JobsCsvExporter().load()
    if job_id not in rows:
        raise ValueError("Unknown job. Refresh the shortlist before updating it.")
    skips = load_skips()
    store = DeltaStore()
    if undo:
        previous = skips.pop(job_id, None)
        if previous is None:
            raise ValueError("This job is not skipped.")
        store.update_status(job_id, previous.get("previous_status") or "scraped")
    else:
        if job_id in skips:
            raise ValueError("This job is already skipped.")
        current = store.statuses([job_id]).get(job_id, "scraped")
        if current == "applied" or current.startswith("replied_"):
            raise ValueError("This job has an application on record and cannot be skipped.")
        row = rows[job_id]
        skips[job_id] = {
            "at": utc_now_iso(), "reason": (reason or "").strip()[:MAX_REASON_CHARS],
            "title": row.get("Title", ""), "company": row.get("Company", ""),
            "previous_status": current,
        }
        store.update_status(job_id, "skipped")
    path = skips_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(skips, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return {"ok": True, "skipped": sorted(skips)}
