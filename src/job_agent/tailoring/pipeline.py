"""Phase 4: dynamic resume tailoring pipeline coordinator.

Orchestrates:

1. Profile loading and fact-seal verification.
2. Ingestion of the qualified jobs from Phase 3.
3. Bullet rewriting behind the anti-hallucination integrity gate.
4. Single-column ATS PDF compilation via Typst.
5. Delta store updates and a manifest recording what the integrity gate did.

The manifest records every restored and every rejected metric, so a tailored PDF
can be audited after the fact without re-running the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from job_agent.config.schema import EvaluatedJob, TailoredResumeRecord
from job_agent.config.settings import settings
from job_agent.intake.validator import load_and_verify_profile
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.tailoring.compiler import TypstResumeCompiler
from job_agent.tailoring.rewriter import ResumeTailorer

console = Console()


class ResumeTailoringPipeline:
    """Coordinator for dynamic ATS resume tailoring and Typst compilation."""

    def __init__(
        self,
        tailorer: Optional[ResumeTailorer] = None,
        compiler: Optional[TypstResumeCompiler] = None,
        delta_store: Optional[DeltaStore] = None,
    ):
        self.tailorer = tailorer or ResumeTailorer()
        self.compiler = compiler or TypstResumeCompiler()
        self.delta_store = delta_store or DeltaStore()

    def run_tailoring(
        self,
        profile_path: Optional[Path] = None,
        qualified_jobs_path: Optional[Path] = None,
        specific_job_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Tailor and compile a bespoke resume for each qualified target job."""
        profile_file = profile_path or settings.profile_path
        qualified_file = qualified_jobs_path or (settings.outputs_dir / "qualified_jobs.json")

        console.print("\n[bold cyan]=== Phase 4: dynamic resume tailoring (Typst) ===[/bold cyan]")

        profile, is_valid = load_and_verify_profile(profile_file)
        if not is_valid:
            # The integrity gate compares rewrites against the profile's locked
            # metrics; if those were tampered with, the gate is checking the wrong
            # reference and cannot be relied on.
            raise ValueError(
                "Refusing to tailor: the profile's fact seal does not verify, so the "
                "anti-hallucination gate cannot be trusted. Re-run 'python main.py intake' "
                "to re-seal the profile from the source resume."
            )

        qualified = self._load_qualified(qualified_file)

        if specific_job_id:
            qualified = [item for item in qualified if item.job.id == specific_job_id]
            if not qualified:
                console.print(f"[bold red]Job ID '{specific_job_id}' is not in the qualified list.[/bold red]")
                return []

        # Highest scoring roles first, so a `--limit` run tailors the best matches.
        qualified.sort(key=lambda item: item.evaluation.fit_score, reverse=True)
        if limit is not None:
            qualified = qualified[:limit]

        if not qualified:
            console.print("[yellow]No qualified jobs to tailor for. Run 'python main.py evaluate' first.[/yellow]")
            return []

        console.print(f"Targeting [bold green]{len(qualified)}[/bold green] qualified jobs.\n")

        records: List[TailoredResumeRecord] = []
        for index, evaluated in enumerate(qualified, start=1):
            job = evaluated.job
            console.print(
                f"[{index}/{len(qualified)}] {job.title} @ {job.company} "
                f"(fit {evaluated.evaluation.fit_score:.1f}/10)"
            )
            try:
                tailored = self.tailorer.generate_tailored_profile_data(profile, job)
                pdf_path = self.compiler.compile_resume(tailored, job_id=job.id)
            except Exception as exc:
                # A Typst failure on one job should not cost the whole batch.
                console.print(f"  [red]Tailoring failed for {job.company}: {exc}[/red]")
                continue

            self.delta_store.update_status(job.id, "tailored")
            integrity = tailored.get("integrity", {})
            records.append(
                TailoredResumeRecord(
                    job_id=job.id,
                    title=job.title,
                    company=job.company,
                    score=evaluated.evaluation.fit_score,
                    pdf_path=str(pdf_path),
                    json_path=str(self.compiler.output_dir / f"tailored_{job.id}.json"),
                    restored_metrics=integrity.get("restored_metrics", []),
                    dropped_fabrications=integrity.get("dropped_fabrications", []),
                )
            )

        results = [record.model_dump() for record in records]
        manifest_path = self.compiler.output_dir / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

        self._render_summary(records, manifest_path)
        return results

    @staticmethod
    def _load_qualified(qualified_file: Path) -> List[EvaluatedJob]:
        """Load the qualified jobs produced by Phase 3."""
        if not qualified_file.exists():
            raise FileNotFoundError(
                f"Qualified jobs file not found: {qualified_file}. Run 'python main.py evaluate' first."
            )
        try:
            raw = json.loads(qualified_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{qualified_file} is not valid JSON: {exc}") from exc

        items: List[EvaluatedJob] = []
        for entry in raw:
            try:
                items.append(EvaluatedJob(**entry))
            except Exception as exc:
                console.print(f"[dim]Skipping malformed qualified entry: {exc}[/dim]")
        return items

    @staticmethod
    def _render_summary(records: List[TailoredResumeRecord], manifest_path: Path) -> None:
        """Print the compilation summary, including anti-hallucination activity."""
        table = Table(title="Tailored resume compilation", show_header=True, header_style="bold magenta")
        table.add_column("Company", style="cyan")
        table.add_column("Role", style="white")
        table.add_column("Fit", justify="center", style="green")
        table.add_column("Integrity gate", style="yellow")
        table.add_column("PDF", style="yellow")

        for record in records:
            notes = []
            if record.restored_metrics:
                notes.append(f"{len(record.restored_metrics)} restored")
            if record.dropped_fabrications:
                notes.append(f"{len(record.dropped_fabrications)} fabrication(s) blocked")
            table.add_row(
                record.company,
                record.title,
                f"{record.score:.1f}/10",
                ", ".join(notes) or "clean",
                Path(record.pdf_path).name,
            )

        console.print(table)
        console.print(f"\n[bold green]Phase 4 complete.[/bold green] {len(records)} tailored PDF(s).")
        console.print(f"Manifest: [yellow]{manifest_path}[/yellow]\n")
