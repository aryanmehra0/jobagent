"""Record a candidate's own manual submission without contacting the employer."""
from __future__ import annotations

import json

from job_agent.config.normalize import utc_now_iso
from job_agent.config.settings import settings
from job_agent.runtime import exclusive_run
from job_agent.tracking.export import JobsCsvExporter, _read_json


@exclusive_run
def mark_applied(job_id: str, undo: bool = False):
    from job_agent.sourcing.delta_store import DeltaStore
    exporter = JobsCsvExporter()
    rows = exporter.load()
    if job_id not in rows:
        raise ValueError("Unknown job. Refresh the shortlist before updating it.")
    path = settings.outputs_dir / "manual_applications.json"
    updates = _read_json(path, {})
    if undo:
        previous = updates.pop(job_id, None)
        if previous is None:
            raise ValueError("Only your own manual applied marker can be undone.")
        rows[job_id]["Status"] = previous.get("previous_status", "manual_apply")
        exporter._write(rows.values())
        DeltaStore().update_status(job_id, previous.get("previous_delta_status", "scraped"))
    else:
        if rows[job_id].get("Status") == "applied":
            raise ValueError("This job is already marked as applied.")
        updates[job_id] = {"status": "applied", "at": utc_now_iso(),
                           "previous_delta_status": DeltaStore().statuses([job_id]).get(job_id, "scraped"),
                           "previous_status": rows[job_id].get("Status", "found")}
        # Historical jobs may no longer appear in collect_records.
        rows[job_id]["Status"] = "applied"
        exporter._write(rows.values())
        DeltaStore().update_status(job_id, "applied")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(updates, indent=2), encoding="utf-8")
    temporary.replace(path)
    from job_agent.workflow import publish_outputs
    return publish_outputs(bundle=True)
