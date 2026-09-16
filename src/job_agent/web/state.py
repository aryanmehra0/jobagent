"""Pipeline state snapshot for the flow console.

Reads the artifacts each phase writes to `data/outputs/` and reports what exists,
how much of it there is, and what the next action would be. This is the same
information `job-agent status` prints, shaped as JSON for the dashboard.

State is derived from files on disk rather than held in memory, so the console
shows the true state of the pipeline even after a restart, and stays correct when
phases are run from the CLI instead of the UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_agent.config.settings import settings

# Phase identifiers, in execution order. The dashboard draws its graph from this.
PHASE_ORDER: List[str] = ["intake", "source", "evaluate", "tailor", "apply", "track"]

# The demo resume bundled with the repository. A profile built from it belongs to
# a fictional candidate, so the console has to say so loudly: running the pipeline
# on it would tailor resumes and send outreach under someone else's name.
SAMPLE_RESUME_NAME = "sample_resume.pdf"

PHASE_META: Dict[str, Dict[str, str]] = {
    "intake": {
        "title": "Resume Intake",
        "subtitle": "PDF to sealed profile",
        "detail": "Extracts your resume into profile.json and seals its facts with SHA-256.",
        "command": "python main.py intake --resume <pdf>",
    },
    "source": {
        "title": "Sourcing",
        "subtitle": "Job boards + ATS feeds",
        "detail": "Scrapes LinkedIn, Indeed and direct ATS boards, then removes jobs already seen.",
        "command": "python main.py source",
    },
    "evaluate": {
        "title": "Evaluation",
        "subtitle": "Embeddings, then LLM judge",
        "detail": "Two-tier matching; only jobs clearing Tier 1 reach the paid LLM judge.",
        "command": "python main.py evaluate",
    },
    "tailor": {
        "title": "Tailoring",
        "subtitle": "Bespoke ATS PDFs",
        "detail": "Rewrites bullets per role behind the anti-hallucination gate, compiles with Typst.",
        "command": "python main.py tailor",
    },
    "apply": {
        "title": "Auto-Apply",
        "subtitle": "Playwright, DOM-only",
        "detail": "Navigates portals and submits. Dry run never opens a browser.",
        "command": "python main.py apply --dry-run",
    },
    "track": {
        "title": "Tracking",
        "subtitle": "Excel + cold outreach",
        "detail": "Logs every outcome to a styled workbook with a personalized email per role.",
        "command": "python main.py track --all",
    },
}


def _read_json(path: Path, default: Any) -> Any:
    """Read a JSON artifact, returning `default` when it is missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _artifact(path: Path) -> Dict[str, Any]:
    """Describe an artifact file for the dashboard's output list."""
    exists = path.exists()
    return {
        "name": path.name,
        "path": str(path),
        "exists": exists,
        "size": path.stat().st_size if exists else 0,
    }


def _intake_state() -> Dict[str, Any]:
    """Profile presence, seal validity, and extraction depth."""
    path = settings.profile_path
    if not path.exists():
        return {
            "status": "empty",
            "summary": "No profile yet",
            "hint": "Upload a resume PDF to begin.",
            "metrics": {},
            "artifacts": [_artifact(path)],
        }

    # Imported lazily: this pulls in pydantic models and is not needed to render
    # an empty dashboard.
    from job_agent.config.schema import CandidateProfile
    from job_agent.intake.validator import describe_profile_gaps

    try:
        profile = CandidateProfile(**json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        return {
            "status": "error",
            "summary": "profile.json is not readable",
            "hint": str(exc)[:200],
            "metrics": {},
            "artifacts": [_artifact(path)],
        }

    sealed = profile.verify_integrity()
    gaps = describe_profile_gaps(profile)
    is_sample = (profile.source_document or "").lower() == SAMPLE_RESUME_NAME

    if not sealed:
        hint = "Fact seal does not verify; re-run intake."
    elif is_sample:
        hint = "This is the bundled demo profile, not you. Upload your own resume."
    else:
        hint = "Fact seal verified."

    return {
        "status": "ready" if sealed else "error",
        "summary": profile.contact.full_name,
        "hint": hint,
        "is_sample": is_sample,
        "metrics": {
            "Roles": len(profile.experience),
            "Years": f"{profile.years_of_experience:g}",
            "Locked facts": len(profile.all_locked_facts()),
            "Skills": len(profile.skills.all_skills()),
        },
        "gaps": gaps,
        "sealed": sealed,
        "extracted_by": profile.extraction_method or "unknown",
        "source_document": profile.source_document,
        "artifacts": [_artifact(path)],
    }


def _source_state() -> Dict[str, Any]:
    """Novel jobs found in the most recent sweep, plus lifetime delta store counts."""
    path = settings.outputs_dir / "scraped_jobs.json"
    jobs = _read_json(path, default=[])

    tracked: Dict[str, int] = {}
    try:
        from job_agent.sourcing.delta_store import DeltaStore

        tracked = DeltaStore().status_counts()
    except Exception:
        pass

    return {
        "status": "ready" if path.exists() else "empty",
        "summary": f"{len(jobs)} novel job(s)" if path.exists() else "Not run yet",
        "hint": "Jobs already seen in earlier sweeps are filtered out.",
        "metrics": {
            "This sweep": len(jobs),
            "Tracked all-time": sum(tracked.values()),
        },
        "breakdown": tracked,
        "artifacts": [_artifact(path)],
    }


def _evaluate_state() -> Dict[str, Any]:
    """How many jobs were scored and how many cleared the fit threshold."""
    evaluated_path = settings.outputs_dir / "evaluated_jobs.json"
    qualified_path = settings.outputs_dir / "qualified_jobs.json"
    evaluated = _read_json(evaluated_path, default=[])
    qualified = _read_json(qualified_path, default=[])

    top = [
        {
            "company": item["job"]["company"],
            "title": item["job"]["title"],
            "score": item["evaluation"]["fit_score"],
            "url": item["job"]["job_url"],
        }
        for item in sorted(
            qualified, key=lambda i: i["evaluation"]["fit_score"], reverse=True
        )[:8]
    ]

    return {
        "status": "ready" if evaluated_path.exists() else "empty",
        "summary": f"{len(qualified)} qualified" if evaluated_path.exists() else "Not run yet",
        "hint": f"Threshold: fit >= {settings.min_match_score:g}/10.",
        "metrics": {"Scored": len(evaluated), "Qualified": len(qualified)},
        "top": top,
        "artifacts": [_artifact(evaluated_path), _artifact(qualified_path)],
    }


def _tailor_state() -> Dict[str, Any]:
    """Compiled resumes and what the anti-hallucination gate did to each."""
    path = settings.outputs_dir / "tailored_resumes" / "manifest.json"
    records = _read_json(path, default=[])

    restored = sum(len(item.get("restored_metrics", [])) for item in records)
    blocked = sum(len(item.get("dropped_fabrications", [])) for item in records)

    return {
        "status": "ready" if path.exists() else "empty",
        "summary": f"{len(records)} tailored PDF(s)" if path.exists() else "Not run yet",
        "hint": "Integrity gate output is recorded per resume.",
        "metrics": {
            "PDFs": len(records),
            "Metrics restored": restored,
            "Fabrications blocked": blocked,
        },
        "resumes": [
            {
                "company": item.get("company"),
                "title": item.get("title"),
                "score": item.get("score"),
                "pdf": Path(item.get("pdf_path", "")).name,
                "restored": item.get("restored_metrics", []),
                "blocked": item.get("dropped_fabrications", []),
            }
            for item in records
        ],
        "artifacts": [_artifact(path)],
    }


def _apply_state() -> Dict[str, Any]:
    """Submitted, simulated, and failed application attempts."""
    path = settings.outputs_dir / "application_results.json"
    data = _read_json(path, default={})
    successful = data.get("successful", [])
    failed = data.get("failed", [])
    dry_runs = sum(1 for item in successful if item.get("status") == "dry_run")

    return {
        "status": "ready" if path.exists() else "empty",
        "summary": (
            f"{len(successful) - dry_runs} submitted, {dry_runs} simulated"
            if path.exists()
            else "Not run yet"
        ),
        "hint": "A dry run never opens a browser and never submits.",
        "metrics": {
            "Submitted": len(successful) - dry_runs,
            "Dry runs": dry_runs,
            "Fallbacks": len(failed),
        },
        "artifacts": [_artifact(path)],
    }


def _track_state() -> Dict[str, Any]:
    """Rows in the master tracking workbook."""
    path = settings.tracker_path
    rows = 0
    if path.exists():
        try:
            import openpyxl

            workbook = openpyxl.load_workbook(str(path), read_only=True)
            rows = max(0, workbook.active.max_row - 1)
            workbook.close()
        except Exception:
            rows = 0

    return {
        "status": "ready" if path.exists() else "empty",
        "summary": f"{rows} logged" if path.exists() else "Not run yet",
        "hint": "Re-running updates each job's row instead of duplicating it.",
        "metrics": {"Rows": rows},
        "artifacts": [_artifact(path)],
    }


_BUILDERS = {
    "intake": _intake_state,
    "source": _source_state,
    "evaluate": _evaluate_state,
    "tailor": _tailor_state,
    "apply": _apply_state,
    "track": _track_state,
}


def config_state() -> Dict[str, Any]:
    """The current search parameters, or the reason they cannot be loaded."""
    from job_agent.intake.cli import load_search_parameters

    path = settings.searches_path
    if not path.exists():
        return {"status": "empty", "path": str(path), "error": None, "values": None}
    try:
        params = load_search_parameters(path)
    except Exception as exc:
        return {"status": "error", "path": str(path), "error": str(exc), "values": None}
    return {"status": "ready", "path": str(path), "error": None, "values": params.model_dump()}


def supported_formats() -> List[str]:
    """Resume file extensions the uploader should accept."""
    from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES

    return list(SUPPORTED_RESUME_SUFFIXES)


def available_resumes() -> List[Dict[str, Any]]:
    """Resume PDFs available for intake, newest first.

    Real resumes sort ahead of the bundled sample regardless of timestamp, so the
    console never preselects demo data when the user has supplied their own.
    """
    candidates = [
        path
        for suffix in supported_formats()
        for path in settings.raw_resumes_dir.glob(f"*{suffix}")
    ]
    pdfs = sorted(
        candidates,
        key=lambda item: (item.name.lower() == SAMPLE_RESUME_NAME, -item.stat().st_mtime),
    )
    return [
        {
            "name": pdf.name,
            "size": pdf.stat().st_size,
            "modified": pdf.stat().st_mtime,
            "is_sample": pdf.name.lower() == SAMPLE_RESUME_NAME,
        }
        for pdf in pdfs
    ]


def build_snapshot() -> Dict[str, Any]:
    """Assemble the full dashboard state.

    Every phase is computed independently so one unreadable artifact shows as a
    single failed node rather than blanking the whole dashboard.
    """
    phases: Dict[str, Any] = {}
    for name in PHASE_ORDER:
        try:
            phases[name] = _BUILDERS[name]()
        except Exception as exc:
            phases[name] = {
                "status": "error",
                "summary": "Could not read state",
                "hint": str(exc)[:200],
                "metrics": {},
                "artifacts": [],
            }
        phases[name].update(PHASE_META[name])

    resumes = available_resumes()
    config = config_state()

    return {
        "phases": phases,
        "order": PHASE_ORDER,
        "config": config,
        "resumes": resumes,
        # Drives the first-run wizard and the demo-data warning banner. Computed
        # here rather than in the browser so the CLI and the console agree on what
        # "ready to run" means.
        "setup": {
            "needs_resume": not any(not item["is_sample"] for item in resumes),
            "needs_profile": phases["intake"]["status"] != "ready",
            "using_sample": bool(phases["intake"].get("is_sample")),
            "needs_config": config["status"] != "ready",
            "ready": (
                phases["intake"]["status"] == "ready"
                and not phases["intake"].get("is_sample")
                and config["status"] == "ready"
            ),
        },
        "formats": supported_formats(),
        "provider": settings.active_provider,
        "thresholds": {
            "tier1": settings.tier1_threshold,
            "fit": settings.min_match_score,
        },
        "paths": {
            "outputs": str(settings.outputs_dir),
            "resumes": str(settings.raw_resumes_dir),
            "tracker": str(settings.tracker_path),
        },
    }
