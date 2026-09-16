"""Autonomous AI job search and application agent - command line interface.

Each phase is a subcommand that reads the previous phase's artifact from
`data/outputs/` and writes its own, so phases can be run individually, re-run, or
chained together with `run-pipeline`.
"""

from __future__ import annotations

import json
import sys

# Ensure UTF-8 console output on Windows before anything prints.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from job_agent.config.settings import settings
from job_agent.intake.cli import configure_cli, load_search_parameters
from job_agent.intake.validator import describe_profile_gaps, load_and_verify_profile

console = Console()


def _fail(message: str, hint: str = "") -> None:
    """Print an error with an optional next step and exit non-zero."""
    console.print(f"[bold red]Error:[/bold red] {message}")
    if hint:
        console.print(f"[dim]{hint}[/dim]")
    sys.exit(1)


def _require(path: Path, what: str, hint: str) -> None:
    """Exit with a clear instruction when a required upstream artifact is missing."""
    if not path.exists():
        _fail(f"{what} not found at {path}.", hint)


@click.group()
@click.version_option("0.2.0", prog_name="job-agent")
def cli() -> None:
    """Autonomous AI job search and application agent."""


# ==============================================================================
# PHASE 1: INTAKE & PARAMETERIZATION
# ==============================================================================

@cli.command("intake")
@click.option(
    "--resume", "-r",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to your resume (.pdf, .docx, .txt or .md).",
)
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Target path for profile.json.",
)
def intake_command(resume: Optional[Path], output: Optional[Path]) -> None:
    """Phase 1: parse your resume into a sealed, fact-locked profile.json."""
    from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES, ResumeParser

    if not resume:
        available = [
            path
            for suffix in SUPPORTED_RESUME_SUFFIXES
            for path in sorted(settings.raw_resumes_dir.glob(f"*{suffix}"))
        ]
        if not available:
            _fail(
                "No resume supplied.",
                f"Pass --resume <path>, or drop a resume into {settings.raw_resumes_dir}. "
                f"Supported: {', '.join(SUPPORTED_RESUME_SUFFIXES)}.",
            )
        resume = available[0]
        console.print(f"[dim]Auto-detected resume: {resume}[/dim]")

    try:
        profile = ResumeParser().parse(pdf_path=resume, output_path=output or settings.profile_path)
    except Exception as exc:
        _fail(f"Resume intake failed: {exc}")

    metrics = sum(len(exp.locked_facts) for exp in profile.experience)
    console.print("\n" + "=" * 62)
    console.print("[bold green]Phase 1 intake complete.[/bold green]")
    console.print(f"Candidate       : [bold]{profile.contact.full_name}[/bold] ({profile.contact.email})")
    console.print(f"Experience      : [cyan]{profile.years_of_experience:g} years[/cyan]")
    console.print(f"Locked facts    : [magenta]{len(profile.experience)} roles, {metrics} metrics[/magenta]")
    console.print(f"Extracted by    : [cyan]{profile.extraction_method}[/cyan]")
    console.print(f"Integrity hash  : [cyan]{profile.fact_hash}[/cyan]")
    console.print("=" * 62)

    gaps = describe_profile_gaps(profile)
    if gaps:
        console.print("\n[yellow]Gaps worth filling in before applying:[/yellow]")
        for gap in gaps:
            console.print(f"  - {gap}")
        console.print(f"[dim]Edit {settings.profile_path} directly, then run 'python main.py verify'.[/dim]")


@cli.command("check")
@click.argument("resume", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def check_command(resume: Optional[Path], as_json: bool) -> None:
    """Diagnose a resume without importing it.

    Reports exactly what the agent could read and, for anything it could not,
    what to change. Nothing is written, so it is safe to run before committing.
    """
    import json as json_module

    from job_agent.intake.readiness import BLOCKER, INFO, WARNING, check_resume

    if not resume:
        from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES

        available = [
            path
            for suffix in SUPPORTED_RESUME_SUFFIXES
            for path in sorted(settings.raw_resumes_dir.glob(f"*{suffix}"))
        ]
        if not available:
            _fail(
                "No resume supplied.",
                f"Pass a path, or drop a file into {settings.raw_resumes_dir}.",
            )
        resume = available[0]
        console.print(f"[dim]Checking {resume.name}[/dim]")

    report = check_resume(resume)

    if as_json:
        console.print_json(json_module.dumps(report.to_dict()))
        sys.exit(0 if report.ready else 1)

    verdict = (
        "[bold green]READY[/bold green]" if report.ready
        else "[bold red]NOT READY[/bold red]"
    )
    console.print(Panel.fit(
        f"{verdict}  ({report.score()}/100)\n"
        f"[dim]{report.document} - {report.layout} - {report.characters} characters[/dim]",
        title="Resume readiness",
        border_style="green" if report.ready else "red",
    ))

    summary = report.summary()
    if summary:
        table = Table(title="What was read", show_header=True, header_style="bold magenta")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        for label, key in (
            ("Name", "name"), ("Email", "email"), ("Phone", "phone"), ("Location", "location"),
            ("Roles", "roles"), ("Education", "education"), ("Projects", "projects"),
            ("Certifications", "certifications"), ("Skills", "skills"),
            ("Quantified achievements", "metrics"), ("Years of experience", "years_of_experience"),
        ):
            value = summary.get(key)
            shown = "[dim]not found[/dim]" if value in (None, "", 0) else str(value)
            table.add_row(label, shown)
        console.print(table)

    icons = {BLOCKER: "[bold red]BLOCKER[/bold red]", WARNING: "[yellow]WARNING[/yellow]", INFO: "[dim]INFO[/dim]"}
    findings = report.sorted_findings()
    if not findings:
        console.print("[bold green]No issues found.[/bold green]")
    else:
        console.print("\n[bold]Findings[/bold]")
        for item in findings:
            console.print(f"  {icons.get(item.severity, item.severity)} [cyan]{item.field}[/cyan]: {item.detail}")
            console.print(f"     [dim]Fix: {item.fix}[/dim]")

    if report.ready:
        console.print(
            f"\n[green]Import it with:[/green] "
            f'python main.py intake --resume "{resume}"'
        )
    sys.exit(0 if report.ready else 1)


@cli.command("configure")
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Target path for searches.yaml.",
)
def configure_command(output: Optional[Path]) -> None:
    """Phase 1: set target domains, locations, freshness, and board constraints."""
    try:
        params = configure_cli(output_path=output or settings.searches_path)
    except Exception as exc:
        _fail(f"Configuration failed: {exc}")

    console.print("\n" + "=" * 62)
    console.print("[bold green]Sourcing parameters configured.[/bold green]")
    console.print(f"Target domains  : [cyan]{', '.join(params.target_domains)}[/cyan]")
    console.print(f"Experience      : [cyan]{params.desired_experience_years:g} years[/cyan]")
    console.print(f"Locations       : [cyan]{', '.join(params.locations)}[/cyan] (remote only: {params.is_remote})")
    console.print(f"Freshness       : [cyan]{params.hours_old} hours[/cyan]")
    console.print(f"Job boards      : [cyan]{', '.join(params.job_boards)}[/cyan]")
    console.print("=" * 62)


@cli.command("verify")
@click.option(
    "--profile", "-p",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to profile.json.",
)
def verify_command(profile: Optional[Path]) -> None:
    """Verify the cryptographic integrity of the candidate's locked facts."""
    try:
        loaded, is_valid = load_and_verify_profile(profile or settings.profile_path)
    except Exception as exc:
        _fail(f"Verification error: {exc}")

    if not is_valid:
        _fail(
            "Verification failed: locked facts were modified outside the intake engine.",
            "Re-run 'python main.py intake' to re-seal the profile from the source resume.",
        )

    console.print(f"[bold green]Profile is sealed and unmutated.[/bold green] SHA-256: {loaded.fact_hash}")
    discrepancy = loaded.experience_discrepancy()
    if discrepancy is not None and discrepancy >= 1.5:
        console.print(
            f"[yellow]Note:[/yellow] stated experience ({loaded.years_of_experience:g} years) differs from the "
            f"role dates ({loaded.computed_years_of_experience():g} years)."
        )


# ==============================================================================
# PHASE 2: SOURCING
# ==============================================================================

@cli.command("source")
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to searches.yaml.",
)
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to save scraped_jobs.json.",
)
@click.option("--no-ats", is_flag=True, help="Skip direct ATS ingestion (Greenhouse, Lever, Ashby).")
def source_command(config: Optional[Path], output: Optional[Path], no_ats: bool) -> None:
    """Phase 2: omnichannel sourcing via JobSpy, direct ATS feeds, and proxy rotation."""
    from job_agent.sourcing.scraper import OmnichannelScraper

    config_path = config or settings.searches_path
    _require(config_path, "searches.yaml", "Run: python main.py configure")

    try:
        params = load_search_parameters(config_path)
        jobs = OmnichannelScraper(search_params=params).run_sourcing_pipeline(
            include_ats_direct=not no_ats, output_file=output
        )
    except Exception as exc:
        _fail(f"Sourcing failed: {exc}")

    console.print(f"[bold green]Sourcing complete.[/bold green] {len(jobs)} novel listing(s).")


# ==============================================================================
# PHASE 3: EVALUATION
# ==============================================================================

@cli.command("evaluate")
@click.option("--profile", "-p", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--jobs", "-j", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option(
    "--threshold", "-t",
    type=click.FloatRange(0.0, 1.0),
    default=None,
    help="Tier 1 embedding similarity cutoff (default: TIER1_THRESHOLD, 0.15).",
)
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Cap how many jobs reach the LLM judge.")
def evaluate_command(
    profile: Optional[Path], jobs: Optional[Path], threshold: Optional[float], limit: Optional[int]
) -> None:
    """Phase 3: two-tier semantic evaluation (embeddings, then an LLM judge)."""
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline

    profile_path = profile or settings.profile_path
    jobs_path = jobs or (settings.outputs_dir / "scraped_jobs.json")
    _require(profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
    _require(jobs_path, "scraped_jobs.json", "Run: python main.py source")

    try:
        SemanticEvaluationPipeline().run_evaluation(
            profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=threshold, limit=limit
        )
    except Exception as exc:
        _fail(f"Evaluation failed: {exc}")


# ==============================================================================
# PHASE 4: TAILORING
# ==============================================================================

@cli.command("tailor")
@click.option("--job-id", "-j", default=None, help="Tailor for one job ID (default: all qualified).")
@click.option("--profile", "-p", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--qualified", "-q", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Tailor only the top N by fit score.")
def tailor_command(
    job_id: Optional[str], profile: Optional[Path], qualified: Optional[Path], limit: Optional[int]
) -> None:
    """Phase 4: dynamic resume tailoring and Typst ATS PDF compilation."""
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    profile_path = profile or settings.profile_path
    qualified_path = qualified or (settings.outputs_dir / "qualified_jobs.json")
    _require(profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
    _require(qualified_path, "qualified_jobs.json", "Run: python main.py evaluate")

    try:
        ResumeTailoringPipeline().run_tailoring(
            profile_path=profile_path,
            qualified_jobs_path=qualified_path,
            specific_job_id=job_id,
            limit=limit,
        )
    except Exception as exc:
        _fail(f"Tailoring failed: {exc}")


# ==============================================================================
# PHASE 5: AUTO-APPLY
# ==============================================================================

@cli.command("apply")
@click.option("--job-id", "-j", default=None, help="Apply to one job ID (default: all tailored).")
@click.option("--dry-run", is_flag=True, help="Simulate without launching a browser or submitting anything.")
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Apply to at most N jobs.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the live-submission confirmation prompt.")
def apply_command(job_id: Optional[str], dry_run: bool, limit: Optional[int], assume_yes: bool) -> None:
    """Phase 5: autonomous browser automation and form submission via Playwright."""
    from job_agent.automation.pipeline import AutoApplyPipeline

    manifest = settings.outputs_dir / "tailored_resumes" / "manifest.json"
    _require(manifest, "Tailored resume manifest", "Run: python main.py tailor")

    try:
        AutoApplyPipeline().run_applications(
            specific_job_id=job_id, dry_run=dry_run, limit=limit, assume_yes=assume_yes
        )
    except Exception as exc:
        _fail(f"Application execution failed: {exc}")


# ==============================================================================
# PHASE 6: TRACKING
# ==============================================================================

@cli.command("track")
@click.option("--all", "-a", "track_all", is_flag=True, help="Log every qualified job, not just the failures.")
def track_command(track_all: bool) -> None:
    """Phase 6: fallback tracking spreadsheet and cold outreach email synthesis."""
    from job_agent.tracking.pipeline import FallbackTrackingPipeline

    try:
        records = FallbackTrackingPipeline().process_fallbacks(force_track_all=track_all)
    except Exception as exc:
        _fail(f"Tracking failed: {exc}")

    console.print(f"[bold green]Tracking complete.[/bold green] {len(records)} entr(ies) written.")


# ==============================================================================
# FULL PIPELINE
# ==============================================================================

@cli.command("run-pipeline")
@click.option("--resume", "-r", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, help="Run every phase but never submit an application.")
@click.option("--skip-intake", is_flag=True, help="Reuse the existing profile.json instead of re-parsing a resume.")
@click.option("--threshold", "-t", type=click.FloatRange(0.0, 1.0), default=None, help="Tier 1 cutoff.")
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Cap jobs per phase after evaluation.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the live-submission confirmation prompt.")
def run_pipeline_command(
    resume: Optional[Path],
    dry_run: bool,
    skip_intake: bool,
    threshold: Optional[float],
    limit: Optional[int],
    assume_yes: bool,
) -> None:
    """Run phases 1 through 6 end to end.

    A phase that produces nothing stops the run cleanly rather than letting the
    next phase fail on a missing artifact.
    """
    from job_agent.automation.pipeline import AutoApplyPipeline
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    from job_agent.intake.parser import ResumeParser
    from job_agent.sourcing.scraper import OmnichannelScraper
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline
    from job_agent.tracking.pipeline import FallbackTrackingPipeline

    console.print(
        Panel.fit(
            "[bold cyan]Autonomous pipeline: phases 1-6[/bold cyan]\n"
            + ("[yellow]Dry run: no application will be submitted.[/yellow]" if dry_run
               else "[red]LIVE run: real applications will be submitted.[/red]"),
            border_style="cyan",
        )
    )

    try:
        # --- Phase 1 ---
        if not skip_intake:
            source_pdf = resume or next(iter(sorted(settings.raw_resumes_dir.glob("*.pdf"))), None)
            if source_pdf:
                ResumeParser().parse(pdf_path=source_pdf, output_path=settings.profile_path)
            elif not settings.profile_path.exists():
                _fail("No resume PDF found and no existing profile.json.", "Pass --resume <path.pdf>.")
        _require(settings.profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
        _require(settings.searches_path, "searches.yaml", "Run: python main.py configure")

        # --- Phase 2 ---
        params = load_search_parameters(settings.searches_path)
        sourced = OmnichannelScraper(search_params=params).run_sourcing_pipeline()
        if not sourced:
            console.print("[yellow]No novel jobs found; stopping here.[/yellow]")
            return

        # --- Phase 3 ---
        _, qualified = SemanticEvaluationPipeline().run_evaluation(
            tier1_threshold=threshold, limit=limit
        )
        if not qualified:
            console.print("[yellow]No jobs met the fit threshold; stopping here.[/yellow]")
            return

        # --- Phase 4 ---
        tailored = ResumeTailoringPipeline().run_tailoring(limit=limit)
        if not tailored:
            console.print("[yellow]No resumes were compiled; stopping here.[/yellow]")
            return

        # --- Phase 5 ---
        AutoApplyPipeline().run_applications(dry_run=dry_run, limit=limit, assume_yes=assume_yes)

        # --- Phase 6 ---
        FallbackTrackingPipeline().process_fallbacks(force_track_all=True)
    except Exception as exc:
        _fail(f"Pipeline halted: {exc}")

    console.print("\n[bold green]Pipeline complete.[/bold green] Review the tracker for next steps:")
    console.print(f"  [yellow]{settings.tracker_path}[/yellow]")


# ==============================================================================
# DIAGNOSTICS
# ==============================================================================

@cli.command("ui")
@click.option("--port", "-p", type=click.IntRange(1024, 65535), default=8765, show_default=True,
              help="Port to serve the flow console on.")
@click.option("--no-browser", is_flag=True, help="Do not open a browser window automatically.")
def ui_command(port: int, no_browser: bool) -> None:
    """Open the visual flow console in your browser.

    Shows the six phases as a node graph, runs them on demand, and streams their
    progress live. It drives the same code these subcommands do, so the two can be
    used interchangeably.
    """
    from job_agent.web.server import run_server

    try:
        run_server(port=port, open_browser=not no_browser)
    except OSError as exc:
        _fail(
            f"Could not start the flow console on port {port}: {exc}",
            "Another process is probably using that port. Try: python main.py ui --port 8790",
        )


@cli.command("doctor")
def doctor_command() -> None:
    """Check the environment: dependencies, API keys, and browser availability."""
    table = Table(title="Environment check", show_header=True, header_style="bold magenta")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_column("Detail", style="white")

    for module, phase, install in (
        ("pdfplumber", "Phase 1 (best-quality PDF text)", "pip install pdfplumber"),
        ("docx", "Phase 1 (.docx resumes)", "pip install python-docx"),
        ("jobspy", "Phase 2 (job boards)", "pip install python-jobspy"),
        ("sentence_transformers", "Phase 3 (dense embeddings)", "pip install sentence-transformers"),
        ("sklearn", "Phase 3 (TF-IDF fallback)", "pip install scikit-learn"),
        ("typst", "Phase 4 (PDF compilation)", "pip install typst"),
        ("playwright", "Phase 5 (browser automation)", "pip install playwright"),
        ("playwright_stealth", "Phase 5 (bot evasion)", "pip install playwright-stealth"),
        ("openpyxl", "Phase 6 (Excel tracker)", "pip install openpyxl"),
    ):
        try:
            __import__(module)
            table.add_row(module, "[green]installed[/green]", phase)
        except ImportError:
            table.add_row(module, "[yellow]missing[/yellow]", f"{phase} - {install}")

    provider = settings.active_provider
    if provider == "none":
        table.add_row(
            "LLM provider",
            "[yellow]not configured[/yellow]",
            "Deterministic fallbacks will be used for every LLM stage.",
        )
    else:
        table.add_row("LLM provider", "[green]ready[/green]", f"{provider} (rerank: {settings.llm_rerank_model})")

    # Playwright needs its browser binaries installed separately from the package.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        if executable.exists():
            table.add_row("Chromium binary", "[green]installed[/green]", str(executable.parent.name))
        else:
            table.add_row("Chromium binary", "[yellow]missing[/yellow]", "Run: python -m playwright install chromium")
    except Exception:
        table.add_row("Chromium binary", "[yellow]unknown[/yellow]", "Run: python -m playwright install chromium")

    table.add_row("Telemetry", "[green]disabled[/green]", "ANONYMIZED_TELEMETRY=false")
    console.print(table)


@cli.command("status")
def status_command() -> None:
    """Show the state of every pipeline phase and its artifacts."""
    from job_agent.sourcing.delta_store import DeltaStore

    console.print(Panel.fit("[bold cyan]Autonomous job search agent - system status[/bold cyan]"))

    table = Table(title="Pipeline state", show_header=True, header_style="bold magenta")
    table.add_column("Phase", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="white")

    # Phase 1a: profile
    if settings.profile_path.exists():
        try:
            profile, valid = load_and_verify_profile(settings.profile_path)
            gaps = describe_profile_gaps(profile)
            detail = (
                f"{profile.contact.full_name} | {len(profile.experience)} roles | "
                f"{profile.years_of_experience:g} yrs | {len(profile.all_locked_facts())} locked facts"
            )
            if gaps:
                detail += f" | {len(gaps)} gap(s)"
            table.add_row(
                "1. Candidate profile",
                "[bold green]sealed[/bold green]" if valid else "[bold red]tampered[/bold red]",
                detail,
            )
        except Exception as exc:
            table.add_row("1. Candidate profile", "[bold red]invalid[/bold red]", str(exc)[:80])
    else:
        table.add_row("1. Candidate profile", "[yellow]missing[/yellow]", "python main.py intake --resume <pdf>")

    # Phase 1b: search parameters
    if settings.searches_path.exists():
        try:
            params = load_search_parameters(settings.searches_path)
            table.add_row(
                "1. Search parameters",
                "[bold green]configured[/bold green]",
                f"{', '.join(params.target_domains)} | {params.hours_old}h | {', '.join(params.job_boards)}",
            )
        except Exception as exc:
            table.add_row("1. Search parameters", "[bold red]invalid[/bold red]", str(exc)[:80])
    else:
        table.add_row("1. Search parameters", "[yellow]missing[/yellow]", "python main.py configure")

    # Phase 2: sourcing
    delta_store = DeltaStore()
    counts = delta_store.status_counts()
    total = sum(counts.values())
    scraped_file = settings.outputs_dir / "scraped_jobs.json"
    breakdown = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())) or "empty"
    table.add_row(
        "2. Sourcing",
        "[bold green]active[/bold green]" if scraped_file.exists() else "[yellow]pending[/yellow]",
        f"{total} tracked | {breakdown}",
    )

    # Phase 3: evaluation
    qualified_file = settings.outputs_dir / "qualified_jobs.json"
    evaluated_file = settings.outputs_dir / "evaluated_jobs.json"
    if qualified_file.exists():
        count = len(_read_json(qualified_file, default=[]))
        table.add_row(
            "3. Evaluation",
            "[bold green]ready[/bold green]" if count else "[yellow]no matches[/yellow]",
            f"{count} qualified (fit >= {settings.min_match_score:g})",
        )
    elif evaluated_file.exists():
        table.add_row("3. Evaluation", "[yellow]no matches[/yellow]", "Nothing met the fit threshold.")
    else:
        table.add_row("3. Evaluation", "[yellow]pending[/yellow]", "python main.py evaluate")

    # Phase 4: tailoring
    manifest_file = settings.outputs_dir / "tailored_resumes" / "manifest.json"
    if manifest_file.exists():
        records = _read_json(manifest_file, default=[])
        blocked = sum(len(item.get("dropped_fabrications", [])) for item in records)
        detail = f"{len(records)} tailored PDF(s)"
        if blocked:
            detail += f" | {blocked} fabricated metric(s) blocked"
        table.add_row("4. Tailoring", "[bold green]ready[/bold green]", detail)
    else:
        table.add_row("4. Tailoring", "[yellow]pending[/yellow]", "python main.py tailor")

    # Phase 5: auto-apply
    results_file = settings.outputs_dir / "application_results.json"
    if results_file.exists():
        data = _read_json(results_file, default={})
        succeeded = data.get("successful", [])
        dry_runs = sum(1 for item in succeeded if item.get("status") == "dry_run")
        table.add_row(
            "5. Auto-apply",
            "[bold green]executed[/bold green]",
            f"submitted: {len(succeeded) - dry_runs} | dry runs: {dry_runs} | fallbacks: {len(data.get('failed', []))}",
        )
    else:
        table.add_row("5. Auto-apply", "[yellow]pending[/yellow]", "python main.py apply --dry-run")

    # Phase 6: tracker
    if settings.tracker_path.exists():
        try:
            import openpyxl

            workbook = openpyxl.load_workbook(str(settings.tracker_path), read_only=True)
            rows = max(0, workbook.active.max_row - 1)
            workbook.close()
            table.add_row("6. Tracker", "[bold green]ready[/bold green]", f"{rows} logged application(s)")
        except Exception as exc:
            table.add_row("6. Tracker", "[yellow]unreadable[/yellow]", str(exc)[:80])
    else:
        table.add_row("6. Tracker", "[yellow]pending[/yellow]", "python main.py track")

    provider = settings.active_provider
    table.add_row(
        "LLM provider",
        "[green]active[/green]" if provider != "none" else "[yellow]none[/yellow]",
        provider if provider != "none" else "Deterministic fallbacks in use.",
    )
    table.add_row("Privacy", "[green]protected[/green]", "ANONYMIZED_TELEMETRY=false")

    console.print(table)


@cli.command("reset")
@click.option("--delta", is_flag=True, help="Clear the delta store so previously seen jobs are sourced again.")
@click.option("--outputs", is_flag=True, help="Delete generated JSON artifacts (keeps the Excel tracker).")
@click.confirmation_option(prompt="This deletes generated pipeline state. Continue?")
def reset_command(delta: bool, outputs: bool) -> None:
    """Clear generated pipeline state so a run can start fresh."""
    from job_agent.sourcing.delta_store import DeltaStore

    if not delta and not outputs:
        _fail("Nothing selected.", "Pass --delta, --outputs, or both.")

    if delta:
        DeltaStore().reset()
        console.print("[green]Delta store cleared; previously seen jobs will be sourced again.[/green]")

    if outputs:
        removed = 0
        for name in ("scraped_jobs.json", "evaluated_jobs.json", "qualified_jobs.json", "application_results.json"):
            path = settings.outputs_dir / name
            if path.exists():
                path.unlink()
                removed += 1
        console.print(f"[green]Removed {removed} generated artifact(s). The Excel tracker was left intact.[/green]")


def _read_json(path: Path, default):
    """Read a JSON artifact, returning `default` if it is missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


if __name__ == "__main__":
    cli()
