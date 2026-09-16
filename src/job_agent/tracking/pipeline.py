"""Phase 6: fallback tracking and cold outreach pipeline coordinator.

Orchestrates:

1. Identifying jobs that were not successfully submitted.
2. Synthesizing a personalized cold outreach email for each.
3. Writing styled records into the master Excel workbook.

Every score and status written here is looked up from the Phase 3 evaluation
output. An earlier revision wrote fixed placeholder scores (7.5 for failures, 8.0
for successes), which meant the spreadsheet's colour-coded priority column — the
one thing a user sorts by — carried no real information.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from job_agent.config.schema import EvaluatedJob, JobPosting
from job_agent.config.settings import settings
from job_agent.intake.validator import load_and_verify_profile
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.tracking.cold_email import ColdEmailGenerator
from job_agent.tracking.tracker import MasterTracker

console = Console()


class FallbackTrackingPipeline:
    """Coordinator for fallback Excel tracking and cold email generation."""

    def __init__(
        self,
        email_generator: Optional[ColdEmailGenerator] = None,
        tracker: Optional[MasterTracker] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self.email_gen = email_generator or ColdEmailGenerator()
        self.tracker = tracker or MasterTracker()
        self.delta_store = delta_store or DeltaStore()

    def process_fallbacks(
        self,
        application_results_path: Optional[Path] = None,
        qualified_jobs_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        force_track_all: bool = False,
    ) -> List[Dict[str, Any]]:
        """Log unsubmitted jobs (and optionally all qualified jobs) with outreach emails.

        Args:
            force_track_all: Also log every qualified role, including ones already
                submitted, so the workbook becomes a complete record of the run.
        """
        results_file = application_results_path or (settings.outputs_dir / "application_results.json")
        qualified_file = qualified_jobs_path or (settings.outputs_dir / "qualified_jobs.json")

        console.print("\n[bold cyan]=== Phase 6: fallback tracking & cold outreach ===[/bold cyan]")

        profile, is_valid = load_and_verify_profile(profile_path or settings.profile_path)
        if not is_valid:
            console.print(
                "[bold red]Profile fact verification failed.[/bold red] "
                "Outreach emails may cite unverified achievements; review them before sending."
            )

        evaluations = self._load_evaluations(qualified_file)
        entries = self._collect_entries(results_file, qualified_file, evaluations, force_track_all)

        if not entries:
            console.print(
                "[yellow]Nothing to track.[/yellow] Run 'python main.py apply' first, "
                "or use '--all' to log every qualified role for manual outreach."
            )
            return []

        console.print(f"Logging [bold yellow]{len(entries)}[/bold yellow] job(s) to the master spreadsheet.\n")

        logged: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries, start=1):
            job: JobPosting = entry["job"]
            score: float = entry["score"]

            console.print(f"[{index}/{len(entries)}] Drafting outreach for {job.title} @ {job.company}...")
            cold_email = self.email_gen.generate_email(profile, job, fit_score=score)

            row = self.tracker.log_application(
                job=job,
                match_score=score,
                status=entry["status"],
                cold_email=cold_email,
                failure_reason=entry["reason"],
                pdf_path=entry.get("pdf_path"),
                # Saved once at the end instead of once per row.
                autosave=False,
            )
            self.delta_store.update_status(job.id, "fallback_logged")

            logged.append({
                "job_id": job.id,
                "company": job.company,
                "title": job.title,
                "score": score,
                "status": entry["status"],
                "row": row,
                "email_snippet": cold_email[:120].replace("\n", " "),
            })

        self.tracker.save()
        self._render_summary(logged)
        return logged

    # --- Entry collection -----------------------------------------------------

    @staticmethod
    def _load_evaluations(qualified_file: Path) -> Dict[str, EvaluatedJob]:
        """Index the Phase 3 evaluations by job ID for score and posting lookup."""
        if not qualified_file.exists():
            return {}
        try:
            raw = json.loads(qualified_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[yellow]Could not read {qualified_file.name}: {exc}[/yellow]")
            return {}

        index: Dict[str, EvaluatedJob] = {}
        for item in raw:
            try:
                evaluated = EvaluatedJob(**item)
            except Exception:
                continue
            index[evaluated.job.id] = evaluated
        return index

    def _collect_entries(
        self,
        results_file: Path,
        qualified_file: Path,
        evaluations: Dict[str, EvaluatedJob],
        force_track_all: bool,
    ) -> List[Dict[str, Any]]:
        """Build the list of jobs to log, with their real scores attached."""
        entries: List[Dict[str, Any]] = []
        seen_ids: set = set()

        if results_file.exists():
            try:
                results = json.loads(results_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                console.print(f"[yellow]Could not read {results_file.name}: {exc}[/yellow]")
                results = {}

            for outcome in results.get("failed", []):
                built = self._entry_from_outcome(
                    outcome,
                    evaluations,
                    status="FALLBACK REQUIRED",
                    reason=outcome.get("error") or "No submission confirmation detected",
                )
                if built:
                    entries.append(built)
                    seen_ids.add(built["job"].id)

            if force_track_all:
                for outcome in results.get("successful", []):
                    built = self._entry_from_outcome(
                        outcome,
                        evaluations,
                        status="APPLIED" if outcome.get("status") == "applied" else "DRY RUN",
                        reason=(
                            "Submitted via browser automation"
                            if outcome.get("status") == "applied"
                            else "Dry run; not actually submitted"
                        ),
                    )
                    if built:
                        entries.append(built)
                        seen_ids.add(built["job"].id)

        # `--all`, or no application run yet: log every qualified role for outreach.
        if force_track_all or not entries:
            if not evaluations and not qualified_file.exists():
                return entries
            for job_id, evaluated in evaluations.items():
                if job_id in seen_ids:
                    continue
                entries.append({
                    "job": evaluated.job,
                    "status": "OUTREACH QUEUED",
                    "reason": "Qualified role queued for direct outreach",
                    "score": evaluated.evaluation.fit_score,
                    "pdf_path": str(
                        settings.outputs_dir / "tailored_resumes" / f"resume_{job_id}.pdf"
                    ),
                })

        return entries

    @staticmethod
    def _entry_from_outcome(
        outcome: Dict[str, Any],
        evaluations: Dict[str, EvaluatedJob],
        status: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        """Turn one application outcome into a tracker entry with its real fit score.

        The posting itself is taken from the Phase 3 evaluation when available,
        because the outcome record carries only a summary of it.
        """
        job_id = outcome.get("job_id") or outcome.get("id")
        evaluated = evaluations.get(job_id) if job_id else None

        if evaluated is not None:
            job = evaluated.job
            score = evaluated.evaluation.fit_score
        else:
            # The evaluation record is gone (outputs cleared); reconstruct what we can.
            try:
                job = JobPosting(
                    id=job_id or "unknown",
                    title=outcome.get("title") or "Unknown role",
                    company=outcome.get("company") or "Unknown company",
                    job_url=outcome.get("job_url") or "",
                    description="",
                    source="portal",
                )
            except Exception as exc:
                console.print(f"[dim]Skipping unusable application record: {exc}[/dim]")
                return None
            score = float(outcome.get("fit_score") or 0.0)

        return {"job": job, "status": status, "reason": reason, "score": score, "pdf_path": outcome.get("pdf_path")}

    # --- Reporting ------------------------------------------------------------

    def _render_summary(self, logged: List[Dict[str, Any]]) -> None:
        """Print what was written to the workbook."""
        table = Table(title="Master spreadsheet tracking summary", show_header=True, header_style="bold magenta")
        table.add_column("Company", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Score", justify="center", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Row", justify="center")
        table.add_column("Outreach hook", style="white")

        for record in logged:
            table.add_row(
                record["company"],
                record["title"],
                f"{record['score']:.1f}",
                record["status"],
                str(record["row"]),
                record["email_snippet"][:60] + "...",
            )

        console.print(table)
        console.print("\n[bold green]Phase 6 complete.[/bold green]")
        console.print(f"Master tracker: [yellow]{self.tracker.excel_path}[/yellow]\n")
