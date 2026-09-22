"""Phase 5: autonomous browser application pipeline coordinator.

Runs the auto-apply agent across every tailored resume, routing confirmed
submissions to the results file and everything else to the Phase 6 fallback.

Before the first *live* submission the pipeline asks for explicit confirmation.
Submitting a job application is irreversible and outward-facing: it puts the
candidate's name in front of a real employer, and a misconfigured run could file
dozens of them. `--dry-run` and `REQUIRE_APPLY_CONFIRMATION=false` both bypass the
prompt for unattended use.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings
from job_agent.runtime import check_cancelled, exclusive_run
from job_agent.automation.agent import AutoApplyAgent
from job_agent.intake.validator import load_and_verify_profile
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.automation.routing import route_application

console = Console()


class AutoApplyPipeline:
    """Coordinates batch application submissions via browser automation."""

    def __init__(
        self,
        agent: Optional[AutoApplyAgent] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self.delta_store = delta_store or DeltaStore()
        self.agent = agent or AutoApplyAgent(delta_store=self.delta_store)

    @exclusive_run
    def run_applications(
        self,
        manifest_path: Optional[Path] = None,
        qualified_jobs_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
        specific_job_id: Optional[str] = None,
        dry_run: bool = False,
        limit: Optional[int] = None,
        assume_yes: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Apply to every tailored job.

        Returns:
            (successful applications, applications routed to the Phase 6 fallback)
        """
        manifest_file = manifest_path or (settings.outputs_dir / "tailored_resumes" / "manifest.json")
        qualified_file = qualified_jobs_path or (settings.outputs_dir / "qualified_jobs.json")
        results_file = output_path or (settings.outputs_dir / "application_results.json")

        console.print("\n[bold cyan]=== Phase 5: autonomous browser execution ===[/bold cyan]")

        profile, is_valid = load_and_verify_profile(profile_path or settings.profile_path)
        if not is_valid:
            raise ValueError(
                "Refusing to apply: the profile's fact seal does not verify. "
                "Applications would carry unverified details. Re-run 'python main.py intake'."
            )

        manifest = self._load_manifest(manifest_file)
        job_lookup, score_lookup = self._load_qualified(qualified_file)

        if specific_job_id:
            manifest = [entry for entry in manifest if entry.get("job_id") == specific_job_id]
            if not manifest:
                console.print(f"[bold red]Job ID '{specific_job_id}' is not in the tailored manifest.[/bold red]")
                return [], []

        if limit is not None:
            manifest = manifest[:limit]

        if not manifest:
            self._write_results(results_file, [], [])
            console.print("[yellow]No tailored resumes to submit. Run 'python main.py tailor' first.[/yellow]")
            return [], []

        if not dry_run and not self._confirm_live_run(manifest, assume_yes):
            console.print("[yellow]Cancelled; nothing was submitted.[/yellow]")
            return [], []

        if not dry_run:
            for entry in manifest:
                pdf = Path(entry["pdf_path"])
                if entry.get("profile_hash") != profile.profile_hash:
                    raise ValueError("Tailored resume belongs to a different or older profile. Run tailor again.")
                if not pdf.is_file() or entry.get("pdf_sha256") != hashlib.sha256(pdf.read_bytes()).hexdigest():
                    raise ValueError("Tailored PDF is missing or has changed. Run tailor again.")
                audit_file = pdf.with_suffix(".ats.json")
                try:
                    audit = json.loads(audit_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError("Tailored PDF has no valid ATS audit. Run tailor again.") from exc
                if audit.get("passed") is not True:
                    raise ValueError("Tailored PDF did not pass its ATS audit. Run tailor again.")

        console.print(f"Targeting [bold green]{len(manifest)}[/bold green] tailored application(s).\n")

        from job_agent.tracking.export import JobsCsvExporter
        already_applied = {key for key, row in JobsCsvExporter().load().items()
                           if row.get('Status') == 'applied' or row.get('Status', '').startswith('replied_')}
        successful: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        try:
            for index, entry in enumerate(manifest, start=1):
                self._check_before_job()
                job_id = entry.get("job_id")
                if job_id in already_applied:
                    console.print(f"[yellow]Already applied or replied: {job_id}; no repeated application.[/yellow]")
                    continue
                job = job_lookup.get(job_id)
                if not job:
                    console.print(f"[yellow]No qualified-job record for ID {job_id}; skipping.[/yellow]")
                    continue

                console.print(f"[{index}/{len(manifest)}] {job.title} @ {job.company}")
                candidate_id = hashlib.sha256(str(profile.contact.email).lower().encode()).hexdigest()
                route = route_application(job)
                if not route.automatable:
                    # Decided before claiming, so a login-walled job stays open for a
                    # later attempt once a public form is known.
                    console.print(f"[yellow]  Manual apply needed: {route.reason}[/yellow]")
                    failed.append({"job_id": job.id, "title": job.title, "company": job.company,
                                   "job_url": job.job_url, "status": "skipped", "steps_taken": 0,
                                   "apply_url": route.url, "channel": route.channel,
                                   "error": route.reason})
                    self._write_results(results_file, successful, failed)
                    continue
                if not dry_run and not self.delta_store.claim_application(candidate_id, job.id):
                    failed.append({"job_id": job.id, "title": job.title, "company": job.company,
                                   "job_url": job.job_url, "status": "skipped", "steps_taken": 0,
                                   "error": "A live attempt already exists; review application history before retrying."})
                    continue
                result = self.agent.apply_to_job(
                    profile=profile,
                    job=job,
                    pdf_resume_path=Path(entry["pdf_path"]),
                    dry_run=dry_run,
                    fit_score=score_lookup.get(job_id),
                )
                if not dry_run:
                    self.delta_store.finish_application(candidate_id, job.id, result)

                if result["status"] in ("applied", "dry_run"):
                    successful.append(result)
                else:
                    failed.append(result)
                self._write_results(results_file, successful, failed)
        finally:
            # Preserve completed outcomes even when a later job or cleanup fails.
            try:
                self._write_results(results_file, successful, failed)
            finally:
                self.agent.close()

        self._render_summary(successful, failed, dry_run)
        return successful, failed

    # --- Helpers --------------------------------------------------------------

    @staticmethod
    def _write_results(results_file: Path, successful: list, failed: list) -> None:
        results_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = results_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"successful": successful, "failed": failed}, indent=2), encoding="utf-8"
        )
        temporary.replace(results_file)

    @staticmethod
    def _check_before_job() -> None:
        """Honour Stop before a new application begins, never during one."""
        check_cancelled()

    @staticmethod
    def _load_manifest(manifest_file: Path) -> List[Dict[str, Any]]:
        """Load the tailored-resume manifest produced by Phase 4."""
        if not manifest_file.exists():
            raise FileNotFoundError(
                f"Tailored resume manifest not found at {manifest_file}. Run 'python main.py tailor' first."
            )
        try:
            entries = json.loads(manifest_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{manifest_file} is not valid JSON: {exc}") from exc
        return [entry for entry in entries if entry.get("job_id") and entry.get("pdf_path")]

    @staticmethod
    def _load_qualified(qualified_file: Path) -> Tuple[Dict[str, JobPosting], Dict[str, float]]:
        """Build job and fit-score lookups keyed by job ID."""
        if not qualified_file.exists():
            raise FileNotFoundError(
                f"Qualified jobs file not found at {qualified_file}. Run 'python main.py evaluate' first."
            )
        raw = json.loads(qualified_file.read_text(encoding="utf-8"))

        jobs: Dict[str, JobPosting] = {}
        scores: Dict[str, float] = {}
        for entry in raw:
            try:
                job = JobPosting(**entry["job"])
            except Exception as exc:
                console.print(f"[dim]Skipping malformed qualified entry: {exc}[/dim]")
                continue
            jobs[job.id] = job
            scores[job.id] = float((entry.get("evaluation") or {}).get("fit_score", 0.0))
        return jobs, scores

    @staticmethod
    def _confirm_live_run(manifest: List[Dict[str, Any]], assume_yes: bool) -> bool:
        """Ask for explicit consent before submitting real applications.

        Skipped when `assume_yes` is set or `REQUIRE_APPLY_CONFIRMATION=false`, so
        scheduled runs are still possible for someone who has opted in.
        """
        if assume_yes or not settings.require_apply_confirmation:
            return True

        companies = ", ".join(dict.fromkeys(entry.get("company", "?") for entry in manifest))
        console.print(
            Panel.fit(
                f"[bold yellow]About to submit {len(manifest)} REAL job application(s).[/bold yellow]\n\n"
                f"Employers: {companies}\n\n"
                "This is not reversible: each submission puts your name and resume in front\n"
                "of a real employer's hiring team. Use [cyan]--dry-run[/cyan] to rehearse first.",
                border_style="yellow",
                title="Confirm live submission",
            )
        )
        try:
            answer = input("Type 'apply' to proceed, anything else to cancel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == "apply"

    @staticmethod
    def _render_summary(
        successful: List[Dict[str, Any]],
        failed: List[Dict[str, Any]],
        dry_run: bool,
    ) -> None:
        """Print the per-application outcome table."""
        table = Table(title="Auto-apply execution summary", show_header=True, header_style="bold magenta")
        table.add_column("Company", style="cyan")
        table.add_column("Role", style="white")
        table.add_column("Status", justify="center")
        table.add_column("Steps", justify="center")
        table.add_column("Details", style="yellow")

        for item in successful:
            label = "[cyan]DRY RUN[/cyan]" if item["status"] == "dry_run" else "[bold green]SUBMITTED[/bold green]"
            detail = "Simulated" if item["status"] == "dry_run" else "Confirmation detected"
            table.add_row(item["company"], item["title"], label, str(item["steps_taken"]), detail)
        for item in failed:
            table.add_row(
                item["company"],
                item["title"],
                "[bold red]FALLBACK[/bold red]",
                str(item["steps_taken"]),
                (item.get("error") or "No confirmation detected")[:70],
            )

        console.print(table)
        console.print("\n[bold green]Phase 5 complete.[/bold green]")
        console.print(
            f"  {'Simulated' if dry_run else 'Submitted'}: [bold green]{len(successful)}[/bold green]  |  "
            f"Routed to Phase 6 fallback: [bold red]{len(failed)}[/bold red]\n"
        )
