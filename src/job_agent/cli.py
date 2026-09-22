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
import shutil
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from job_agent.config.settings import settings
from job_agent.runtime import exclusive_run
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
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Autonomous AI job search and application agent."""
    import time
    from datetime import datetime, timezone

    # When this phase started, for the run history kept in the jobs database.
    ctx.obj = {"started_at": datetime.now(timezone.utc).isoformat(), "clock": time.perf_counter()}


# ==============================================================================
# PHASE 1: INTAKE & PARAMETERIZATION
# ==============================================================================

_PHASE_COMMANDS = {"source", "evaluate", "tailor", "apply", "track", "prep", "contacts", "run-pipeline"}


@cli.result_callback()
@click.pass_context
def _refresh_jobs_sheet(ctx: click.Context, *_: object, **__: object) -> None:
    """Keep the jobs sheet and the database current after any phase that changes the artifacts."""
    if ctx.invoked_subcommand not in _PHASE_COMMANDS:
        return
    if ctx.invoked_subcommand == "run-pipeline":
        return  # The shared runner exports after each phase and publishes the ZIP.
    import time
    from datetime import datetime, timezone

    from job_agent.storage.jobs_db import record_phase
    from job_agent.workflow import publish_outputs

    stats = publish_outputs(bundle=True)
    for warning in stats["warnings"]:
        console.print(f"[yellow]{warning}[/yellow]")
    started = (ctx.obj or {}).get("started_at")
    record_phase(
        ctx.invoked_subcommand, "warning" if stats["warnings"] else "ok", started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=round(time.perf_counter() - (ctx.obj or {}).get("clock", time.perf_counter()), 2),
        summary=stats, started_from="cli",
    )


@cli.group("db")
def db_group() -> None:
    """Query the database of jobs the agent has fetched."""


@db_group.command("sync")
def db_sync_command() -> None:
    """Rebuild the jobs database from the current artifacts."""
    from job_agent.storage.jobs_db import JobsDatabase

    database = JobsDatabase()
    try:
        stats = database.sync()
    except Exception as exc:
        _fail(f"Database sync failed: {exc}")
    console.print(
        f"[bold green]Synced[/bold green] {stats['jobs']} job(s), {stats['contacts']} contact email(s) "
        f"and {stats['outreach']} outreach draft(s) to [yellow]{database.location}[/yellow]"
    )


@db_group.command("stats")
def db_stats_command() -> None:
    """Show what the database knows: jobs by stage, source and contact coverage."""
    from job_agent.storage.jobs_db import JobsDatabase

    try:
        summary = JobsDatabase().stats()
    except Exception as exc:
        _fail(f"Could not read the database: {exc}")

    console.print(f"[bold cyan]Jobs database[/bold cyan] ({summary['backend']}): {summary['location']}\n")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Measure", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Jobs stored", str(summary["jobs"]))
    table.add_row("With a contact email", str(summary["jobs_with_email"]))
    table.add_row("With an HR / careers email", str(summary["jobs_with_hiring_email"]))
    table.add_row("Outreach drafts", str(summary["outreach_drafts"]))
    for stage, count in sorted(summary["by_status"].items(), key=lambda item: -item[1]):
        table.add_row(f"Stage: {stage}", str(count))
    for source, count in sorted(summary["by_source"].items(), key=lambda item: -item[1]):
        table.add_row(f"Source: {source}", str(count))
    for company, count in summary["top_companies"].items():
        table.add_row(f"Company: {company}", str(count))
    console.print(table)


@db_group.command("jobs")
@click.option("--limit", "-n", type=click.IntRange(1), default=20, help="How many jobs to show.")
@click.option("--status", help="Only this stage, e.g. qualified, tailored, manual_apply.")
@click.option("--company", help="Match part of a company name.")
@click.option("--with-email", is_flag=True, help="Only jobs that have a contact email.")
@click.option("--min-score", type=float, help="Only jobs at or above this fit score.")
def db_jobs_command(limit, status, company, with_email, min_score) -> None:
    """List fetched jobs with their emails and apply routes."""
    from job_agent.storage.jobs_db import JobsDatabase

    try:
        rows = JobsDatabase().jobs(limit=limit, status=status, company=company,
                                   with_email=with_email, min_score=min_score)
    except Exception as exc:
        _fail(f"Could not read the database: {exc}")
    if not rows:
        console.print("[yellow]No jobs matched.[/yellow] Run a search first: python main.py source")
        return

    table = Table(show_header=True, header_style="bold magenta")
    for column in ("Fit", "Role", "Company", "Location", "Email", "Apply", "Stage"):
        table.add_column(column, overflow="fold")
    for row in rows:
        score = row.get("fit_score")
        table.add_row(
            "-" if score is None else f"{float(score):.1f}",
            str(row.get("title") or "")[:40], str(row.get("company") or "")[:24],
            str(row.get("location") or "")[:22], str(row.get("contact_email") or "-"),
            str(row.get("apply_method") or "").replace("_", " "), str(row.get("status") or ""),
        )
    console.print(table)


@db_group.command("resume")
@click.argument("job_id")
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Where to save the PDF (default: your Downloads folder).")
def db_resume_command(job_id: str, out) -> None:
    """Save the tailored resume stored in the database for a job, ready to attach."""
    import hashlib

    from job_agent.storage.jobs_db import JobsDatabase

    stored = JobsDatabase().resume_pdf(job_id)
    if stored is None:
        _fail(f"No tailored resume stored for job {job_id}.", "Run: python main.py db sync")
    if hashlib.sha256(stored["pdf"]).hexdigest() != stored["sha256"]:
        _fail("The stored resume does not match its checksum; run: python main.py db sync")
    target = out or (Path.home() / "Downloads" / stored["file_name"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stored["pdf"])
    console.print(f"[bold green]Saved[/bold green] {target} ({len(stored['pdf']):,} bytes)")
    if stored.get("resume_check"):
        console.print(f"Check: {stored['resume_check']}")


@db_group.command("query")
@click.argument("sql")
@click.option("--limit", "-n", type=click.IntRange(1), default=50, help="Rows to print (default 50).")
@click.option("--csv", "csv_out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the full result to a CSV file instead of printing it.")
def db_query_command(sql: str, limit: int, csv_out) -> None:
    """Run a read-only SQL query against the jobs database.

    Example: python main.py db query "SELECT company, contact_email FROM job_overview WHERE fit_score >= 7"
    """
    from job_agent.storage.jobs_db import JobsDatabase

    statement = sql.strip().rstrip(";")
    if not statement.lower().startswith(("select", "with")):
        _fail("Only SELECT queries are allowed here.",
              "The database is rebuilt by the pipeline; edit jobs through the agent, not by hand.")

    database = JobsDatabase()
    try:
        with database._connect() as conn:
            rows = [dict(row) for row in conn.execute(database._sql(statement)).fetchall()]
    except Exception as exc:
        _fail(f"Query failed: {exc}")

    if not rows:
        console.print("[yellow]No rows.[/yellow]")
        return

    if csv_out:
        import csv as csv_module

        csv_out.parent.mkdir(parents=True, exist_ok=True)
        with csv_out.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_module.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"[bold green]Wrote {len(rows)} row(s) to[/bold green] [yellow]{csv_out}[/yellow]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    for column in rows[0]:
        table.add_column(str(column), overflow="fold")
    for row in rows[:limit]:
        table.add_row(*["" if value is None else str(value)[:60] for value in row.values()])
    console.print(table)
    if len(rows) > limit:
        console.print(f"[dim]{len(rows) - limit} more row(s); raise --limit or use --csv.[/dim]")


@cli.command("preferences")
@click.option("--country", help="Country you live in, e.g. India.")
@click.option("--authorized", help="Countries you may work in without a visa, comma separated.")
@click.option("--sponsorship/--no-sponsorship", default=None,
              help="Whether you need visa sponsorship to work in other countries.")
@click.option("--remote-worldwide/--no-remote-worldwide", default=None,
              help="Open to remote jobs with employers in any country.")
@click.option("--salary", type=int, help="Expected yearly salary (bottom of range), e.g. 1000000.")
@click.option("--salary-max", type=int, help="Top of the expected salary range, e.g. 1400000.")
@click.option("--currency", help="Salary currency, e.g. INR.")
def preferences_command(country, authorized, sponsorship, remote_worldwide, salary, salary_max, currency) -> None:
    """Set country, work authorization, sponsorship and salary (kept across resume uploads)."""
    from job_agent.intake.preferences import load_preferences, save_preferences

    current = load_preferences()
    values = current.model_dump() if current else {}
    updates = {
        "current_country": country, "authorized_countries": authorized, "requires_sponsorship": sponsorship,
        "remote_worldwide": remote_worldwide, "desired_salary": salary, "desired_salary_max": salary_max,
        "salary_currency": currency,
    }
    values.update({key: value for key, value in updates.items() if value is not None})
    try:
        profile = save_preferences(values)
    except Exception as exc:
        _fail(f"Could not save preferences: {exc}")
    auth = profile.work_authorization
    console.print("[bold green]Preferences saved to your profile.[/bold green]")
    console.print(f"  Country        : {auth.current_country}")
    console.print(f"  Authorized in  : {', '.join(auth.authorized_countries) or 'not stated'}")
    console.print(f"  Sponsorship    : {'needed abroad' if auth.requires_sponsorship else 'not needed' if auth.requires_sponsorship is False else 'not stated'}")
    console.print(f"  Remote anywhere: {auth.remote_worldwide}")
    console.print(f"  Salary         : {profile.salary_expectation_text() or 'not stated'}")


@cli.command("export")
@click.option("--bundle", is_flag=True, help="Create a portable ZIP with CSV, verified PDFs and a clickable index.")
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to write the CSV (default: data/outputs/jobs_master.csv).",
)
def export_command(output: Optional[Path], bundle: bool = False) -> None:
    """Write every found job, with contact emails and apply links, to a CSV sheet."""
    from job_agent.tracking.export import JobsCsvExporter

    if bundle:
        from job_agent.tracking.bundle import build_application_pack
        try:
            path = build_application_pack()
            if output and output.resolve() != path.resolve():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, output)
                path = output
        except Exception as exc:
            _fail(f"Pack export failed: {exc}")
        console.print(f"[bold green]Application pack:[/bold green] {path}")
        return

    try:
        path = JobsCsvExporter(csv_path=output).export()
    except Exception as exc:
        _fail(f"Export failed: {exc}")
    import csv
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = sum(1 for _ in csv.DictReader(handle))
    console.print(f"[bold green]Exported {rows} job(s) to[/bold green] [yellow]{path}[/yellow]")


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
@click.option("--cover-letter", is_flag=True, help="Also prepare a grounded one-page cover letter.")
@click.option("--mode", type=click.Choice(["auto", "faithful", "generated", "regional"]), default=None)
@click.option("--country", default=None, help="Override the resume's target market, e.g. India or UK.")
@click.option("--job-id", "-j", default=None, help="Tailor for one job ID (default: all qualified).")
@click.option("--profile", "-p", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--qualified", "-q", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Tailor only the top N by fit score.")
@click.option("--rebuild-existing", is_flag=True,
              help="Re-tailor every job that already has a resume listed (this batch and earlier ones).")
def tailor_command(
    job_id: Optional[str], profile: Optional[Path], qualified: Optional[Path], limit: Optional[int],
    rebuild_existing: bool, mode: Optional[str] = None, country: Optional[str] = None, cover_letter: bool = False,
) -> None:
    """Phase 4: tailor your resume for each qualified job, and validate every PDF."""
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    profile_path = profile or settings.profile_path
    if rebuild_existing:
        _require(profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
        try:
            summary = ResumeTailoringPipeline().rebuild_existing(profile_path=profile_path)
        except Exception as exc:
            _fail(f"Rebuild failed: {exc}")
        console.print(
            f"[bold green]Rebuilt {summary['rebuilt']} resume(s)[/bold green], "
            f"{summary['passed']} passed every check."
        )
        return
    qualified_path = qualified or (settings.outputs_dir / "qualified_jobs.json")
    _require(profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
    _require(qualified_path, "qualified_jobs.json", "Run: python main.py evaluate")

    try:
        ResumeTailoringPipeline().run_tailoring(
            profile_path=profile_path,
            qualified_jobs_path=qualified_path,
            specific_job_id=job_id,
            limit=limit,
            mode=mode,
            country=country,
            cover_letter=cover_letter,
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


@cli.command("workday-assist")
@click.option("--job-id", required=True)
@exclusive_run
def workday_assist_command(job_id):
    """Fill supported Workday fields in a visible browser; never click final Submit."""
    from job_agent.automation.agent import AutoApplyAgent
    from job_agent.automation.routing import is_workday
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline
    from job_agent.tracking.export import _read_json
    import hashlib
    profile, valid = load_and_verify_profile(settings.profile_path)
    if not valid:
        _fail('Profile seal failed.')
    jobs = ResumeTailoringPipeline._load_qualified(settings.outputs_dir/'qualified_jobs.json')
    job = next((j.job for j in jobs if j.job.id == job_id), None)
    if job is None or not (is_workday(job.job_url) or is_workday(job.apply_url)):
        _fail('Select a qualified Workday job.')
    record = next((r for r in _read_json(settings.outputs_dir/'tailored_resumes/manifest.json', []) if r.get('job_id') == job_id), {})
    pdf = settings.outputs_dir/'tailored_resumes'/f'resume_{job_id}.pdf'
    if (not pdf.is_file() or record.get('profile_hash') != profile.profile_hash or
            not record.get('validation_passed') or record.get('pdf_sha256') != hashlib.sha256(pdf.read_bytes()).hexdigest()):
        _fail('Generate a current validated resume first.')
    def review(page, reason):
        click.echo(reason)
        click.pause('The browser remains open for your action. Follow the message above, then press any key here to continue or finish assistance.')
    previous = settings.playwright_headless
    settings.playwright_headless = False
    agent = AutoApplyAgent()
    try:
        console.print(agent.apply_to_job(profile, job, pdf, assist_workday=True, review_callback=review))
    finally:
        agent.close()
        settings.playwright_headless = previous


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


@cli.command("prep")
@click.option("--job-id", default=None)
@click.option("--limit", type=click.IntRange(1), default=None)
@click.option("--offline", is_flag=True, help="Select evidence without provider calls.")
def prep_command(job_id, limit, offline):
    """Phase 7: interview questions, STAR evidence and role briefing."""
    from job_agent.interview.pipeline import InterviewPrepPipeline
    try:
        records = InterviewPrepPipeline().run(job_id=job_id, limit=limit, use_llm=not offline)
        console.print(f"Prepared {len(records)} interview guides.")
    except Exception as exc:
        _fail(str(exc))


@cli.command("sync-inbox")
@click.option("--days", type=click.IntRange(1, 90), default=14)
@click.option("--limit", type=click.IntRange(1, 500), default=100)
def sync_inbox_command(days, limit):
    """Read only the configured IMAP folder and record matched replies."""
    from job_agent.tracking.inbox import sync_inbox
    try:
        console.print(sync_inbox(days=days, limit=limit))
    except Exception as exc:
        _fail(str(exc))


@cli.command("hosted-key")
@click.option("--user", default=None, help="Issue a new key for this hosted user.")
@click.option("--revoke", default=None, help="Revoke a key ID (the part before the dot).")
def hosted_key_command(user, revoke):
    """Local administrator command; keys are displayed once and stored hashed."""
    from job_agent.hosted.auth import HostedIdentityStore
    if bool(user) == bool(revoke):
        _fail('Specify exactly one of --user or --revoke.')
    store = HostedIdentityStore()
    if revoke:
        if not store.revoke(revoke):
            _fail('Unknown key ID.')
        console.print('Key revoked.')
    else:
        try:
            click.echo(store.issue_key(user))
        except ValueError as exc:
            _fail(str(exc))


@cli.command("contacts")
@click.option("--job-id", default=None)
@click.option("--limit", type=click.IntRange(1, 25), default=5)
def contacts_command(job_id, limit):
    """Find published team members as possible, unverified contact leads."""
    from job_agent.contacts.warm import discover
    try:
        console.print(discover(job_id=job_id, limit=limit))
    except Exception as exc:
        _fail(str(exc))


# ==============================================================================
# FULL PIPELINE
# ==============================================================================

@cli.command("run-pipeline")
@click.option("--cover-letter", is_flag=True, help="Include optional cover letters during tailoring.")
@click.option("--resume", "-r", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--dry-run/--live", default=True, help="Default: prepare results without submitting. --live enables applications.")
@click.option("--mode", type=click.Choice(["auto", "faithful", "generated", "regional"]), default="regional", show_default=True)
@click.option("--skip-intake", is_flag=True, help="Reuse the existing profile.json instead of re-parsing a resume.")
@click.option("--threshold", "-t", type=click.FloatRange(0.0, 1.0), default=None, help="Tier 1 cutoff.")
@click.option("--limit", "-n", type=click.IntRange(1), default=None, help="Cap jobs per phase after evaluation.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the live-submission confirmation prompt.")
@exclusive_run
def run_pipeline_command(
    resume: Optional[Path],
    dry_run: bool,
    skip_intake: bool,
    threshold: Optional[float],
    limit: Optional[int],
    assume_yes: bool,
    mode: str = "regional",
    cover_letter: bool = False,
) -> None:
    """Run phases 1 through 7 end to end.

    A phase that produces nothing stops the run cleanly rather than letting the
    next phase fail on a missing artifact.
    """
    from job_agent.web.runner import PipelineRunner

    console.print(
        Panel.fit(
            "[bold cyan]Autonomous pipeline: phases 1-7[/bold cyan]\n"
            + ("[yellow]Dry run: no application will be submitted.[/yellow]" if dry_run
               else "[red]LIVE run: real applications will be submitted.[/red]"),
            border_style="cyan",
        )
    )

    phases = ["source", "evaluate", "tailor", "apply", "track", "prep"]
    if not skip_intake:
        phases.insert(0, "intake")
    else:
        _require(settings.profile_path, "profile.json", "Run: python main.py intake --resume <pdf>")
    result = PipelineRunner().run_sync(phases, {
        "resume": str(resume) if resume else None, "dry_run": dry_run,
        "threshold": threshold, "limit": limit, "tailoring_mode": mode,
        "track_all": True, "assume_yes": assume_yes, "started_from": "cli",
        "cover_letter": cover_letter,
    })
    report = result.get("report", {})
    console.print(f"\nPipeline status: [bold]{result.get('status', 'error')}[/bold]")
    if report.get("halt_reason"):
        console.print(report["halt_reason"])
    for label, path in report.get("files", {}).items():
        console.print(f"  {label}: {path}")
    for warning in report.get("warnings", []):
        console.print(f"[yellow]{warning}[/yellow]")
    if result.get("status") == "error":
        _fail(result.get("error") or "Pipeline failed; review data/outputs/run_report.json.")


# ==============================================================================
# DIAGNOSTICS
# ==============================================================================

@cli.command("ui")
@click.option("--port", "-p", type=click.IntRange(1024, 65535), default=8765, show_default=True,
              help="Port to serve the flow console on.")
@click.option("--no-browser", is_flag=True, help="Do not open a browser window automatically.")
def ui_command(port: int, no_browser: bool) -> None:
    """Open the visual flow console in your browser.

    Shows the seven phases as a node graph, runs them on demand, and streams their
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
@click.option("--live", is_flag=True, help="Test Groq with a small non-personal prompt.")
def doctor_command(live: bool = False) -> None:
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
        table.add_row("LLM provider", "[green]configured[/green]", f"{provider} (rerank: {settings.model_for('rerank')})")

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
    if live:
        if provider != "groq":
            _fail("Live provider check currently requires DEFAULT_LLM_PROVIDER=groq.")
        from job_agent.llm import groq_complete, LLMError
        try:
            reply = groq_complete('Return JSON {"ok": true}.', "Connection check.", max_tokens=256)
            if reply.get("ok") is not True:
                _fail("Groq returned an unexpected connection-check response.")
            console.print("[green]Live Groq connection verified.[/green]")
        except LLMError as exc:
            _fail(str(exc))


@cli.command("production-check")
def production_check_command() -> None:
    """Report whether the project is ready for an end-to-end or self-hosted run."""
    checks = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    add(".env ignored", _git_ignores(".env"), ".env should never be committed.", "Keep .env in .gitignore.")
    add("Profile", settings.profile_path.exists(), str(settings.profile_path), "Run: python main.py intake --resume <file>")
    if settings.profile_path.exists():
        try:
            profile, valid = load_and_verify_profile(settings.profile_path)
            add("Profile seal", valid, f"{profile.contact.full_name} ({profile.contact.email})",
                "Re-run intake from the source resume.")
            add("Profile is not demo", (profile.source_document or "").lower() != "sample_resume.pdf",
                profile.source_document or "unknown", "Upload your real resume in the dashboard.")
        except Exception as exc:
            add("Profile seal", False, str(exc), "Run: python main.py intake --resume <file>")

    add("Search config", settings.searches_path.exists(), str(settings.searches_path), "Run: python main.py configure")
    if settings.searches_path.exists():
        try:
            params = load_search_parameters(settings.searches_path)
            ats_companies = (
                params.ats_companies.model_dump()
                if hasattr(params.ats_companies, "model_dump")
                else dict(params.ats_companies or {})
            )
            add("Search sources", bool(params.job_boards or params.public_sources or params.ats_companies),
                f"boards={len(params.job_boards)}, public={len(params.public_sources)}, ats={sum(len(v) for v in ats_companies.values())}",
                "Enable at least one board, public feed, or ATS company.")
            add("Work modes", bool(params.selected_work_modes), ", ".join(params.selected_work_modes),
                "Set work_modes to remote, hybrid, onsite, or a combination.")
        except Exception as exc:
            add("Search config valid", False, str(exc), "Fix config/searches.yaml.")

    add("LLM provider", settings.active_provider != "none", settings.active_provider,
        "Optional: set DEFAULT_LLM_PROVIDER=groq and GROQ_API_KEYS for better scoring and emails.")
    add("Typst", shutil.which("typst") is not None or _module_exists("typst"), "Needed for ATS PDFs.",
        "Install requirements, or install the Typst binary.")
    add("Chromium", _chromium_installed(), "Needed for browser apply mode.",
        "Run: python -m playwright install chromium")
    add("Dockerfile", (settings.base_dir / "Dockerfile").exists(), "Self-host deployment recipe.",
        "Keep Dockerfile in the repository.")
    add("Local compose", (settings.base_dir / "docker-compose.yml").exists(), "Personal CLI deployment.",
        "Keep docker-compose.yml in the repository.")
    add("Hosted compose", (settings.base_dir / "docker-compose.hosted.yml").exists(), "API + worker reference deployment.",
        "Keep docker-compose.hosted.yml in the repository.")
    add("Hosted env example", (settings.base_dir / "config" / "hosted.example.env").exists(),
        "Documented hosted environment variables.", "Keep config/hosted.example.env in the repository.")
    add("Deployment guide", (settings.base_dir / "DEPLOYMENT.md").exists(), "Hosting and scale instructions.",
        "Keep DEPLOYMENT.md in the repository.")

    table = Table(title="Production readiness", show_header=True, header_style="bold magenta")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="white")
    table.add_column("Next step", style="white")
    for item in checks:
        table.add_row(
            item["name"],
            "[green]ok[/green]" if item["ok"] else "[yellow]attention[/yellow]",
            item["detail"],
            "" if item["ok"] else item["fix"],
        )
    console.print(table)

    blockers = [item for item in checks if not item["ok"] and item["name"] not in {"LLM provider"}]
    if blockers:
        sys.exit(1)


@cli.command("db-check")
def db_check_command() -> None:
    """Check the configured hosted queue database and create its table if needed."""
    from job_agent.hosted.queue import HostedQueue

    try:
        queue = HostedQueue()
        counts = queue.counts()
    except Exception as exc:
        _fail(f"Database check failed: {exc}")

    table = Table(title="Database check", show_header=True, header_style="bold magenta")
    table.add_column("Item", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Backend", queue.backend)
    if queue.backend == "postgres":
        table.add_row("DATABASE_URL", _mask_database_url(settings.database_url or ""))
        table.add_row("Table", "hosted_runs")
    else:
        table.add_row("SQLite file", str(queue.db_path))
        table.add_row("Table", "hosted_runs")
    table.add_row("Queue counts", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty")
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
    outreach_counts = delta_store.outreach_counts()
    if settings.tracker_path.exists():
        try:
            import openpyxl

            workbook = openpyxl.load_workbook(str(settings.tracker_path), read_only=True)
            rows = max(0, workbook.active.max_row - 1)
            workbook.close()
            table.add_row(
                "6. Tracker",
                "[bold green]ready[/bold green]",
                f"{rows} logged | {outreach_counts['drafts']} email draft(s) | "
                f"{outreach_counts['recipients']} unique inbox(es)",
            )
        except Exception as exc:
            table.add_row("6. Tracker", "[yellow]unreadable[/yellow]", str(exc)[:80])
    else:
        table.add_row(
            "6. Tracker",
            "[yellow]pending[/yellow]",
            f"python main.py track | {outreach_counts['drafts']} email draft(s) already in ledger",
        )

    from job_agent.tracking.supplements import document_links
    documents = document_links()
    guide_count = sum('Interview Prep' in value for value in documents.values())
    letter_count = sum('Cover Letter' in value for value in documents.values())
    table.add_row('7. Interview prep', '[green]ready[/green]' if guide_count else '[yellow]pending[/yellow]',
                  f'{guide_count} verified guides | python main.py prep --offline')
    table.add_row('Cover letters', 'optional', f'{letter_count} verified PDFs | tailor --cover-letter')
    table.add_row('Download pack', 'ready' if (settings.outputs_dir/'application_pack.zip').is_file() else 'pending',
                  'Jobs & downloads > Download everything (ZIP); extract, then open index.html')
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


def _module_exists(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


def _git_ignores(path: str) -> bool:
    import fnmatch
    import subprocess

    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=settings.base_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    gitignore = settings.base_dir / ".gitignore"
    if not gitignore.exists():
        return False
    try:
        for raw_line in gitignore.read_text(encoding="utf-8").splitlines():
            pattern = raw_line.strip()
            if not pattern or pattern.startswith("#") or pattern.startswith("!"):
                continue
            normalized = pattern.rstrip("/")
            if normalized == path or fnmatch.fnmatch(path, normalized):
                return True
    except Exception:
        return False
    return False


def _mask_database_url(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.split("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


if __name__ == "__main__":
    cli()
