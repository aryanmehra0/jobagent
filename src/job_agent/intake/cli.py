"""Interactive search parameterization.

Prompts for the targeting constraints that drive Phase 2 and serializes them to
`searches.yaml`. Every answer is validated through `SearchParameters` before the
file is written, and an invalid answer is re-prompted rather than saved, so a
typo in a board name cannot silently produce an empty sourcing sweep later.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from job_agent.config.schema import SUPPORTED_JOB_BOARDS, SUPPORTED_PUBLIC_SOURCES, SearchParameters
from job_agent.config.settings import settings

console = Console()


def _split_list(value: str) -> List[str]:
    """Split a comma-separated prompt answer into a clean list."""
    return [item.strip() for item in value.split(",") if item.strip()]


def prompt_user_parameters(existing_config: Optional[SearchParameters] = None) -> SearchParameters:
    """Gather job search parameters interactively, re-prompting until they validate."""
    console.print(
        Panel.fit(
            "[bold cyan]Omnichannel job search parameterization[/bold cyan]\n"
            "Define your target domain, geography, and freshness constraints.",
            border_style="cyan",
        )
    )

    while True:
        domains = _split_list(
            Prompt.ask(
                "[bold green]?[/bold green] Target job title(s) or domain(s), comma-separated",
                default=", ".join(existing_config.target_domains) if existing_config else "Software Engineer",
            )
        )

        desired_experience = FloatPrompt.ask(
            "[bold green]?[/bold green] Desired experience level (years)",
            default=existing_config.desired_experience_years if existing_config else 3.0,
        )

        locations = _split_list(
            Prompt.ask(
                "[bold green]?[/bold green] Locations, comma-separated (e.g. 'Remote, New York, NY')",
                default=", ".join(existing_config.locations) if existing_config else "Remote",
            )
        )

        work_modes = _split_list(
            Prompt.ask(
                "[bold green]?[/bold green] Work modes (remote, hybrid, onsite)",
                default=", ".join(existing_config.selected_work_modes) if existing_config else "remote",
            )
        )
        onsite_countries = _split_list(
            Prompt.ask(
                "[bold green]?[/bold green] Countries allowed for onsite/hybrid roles (optional)",
                default=", ".join(existing_config.onsite_countries) if existing_config else "",
            )
        )

        hours_old = IntPrompt.ask(
            "[bold green]?[/bold green] Maximum posting age in hours (24, 48, 72...)",
            default=existing_config.hours_old if existing_config else 48,
        )

        job_boards = _split_list(
            Prompt.ask(
                f"[bold green]?[/bold green] Job boards, comma-separated ({', '.join(SUPPORTED_JOB_BOARDS)})",
                default=", ".join(existing_config.job_boards) if existing_config else "linkedin, indeed",
            )
        )
        public_sources = _split_list(
            Prompt.ask(
                f"[bold green]?[/bold green] Public APIs ({', '.join(SUPPORTED_PUBLIC_SOURCES)}; optional)",
                default=", ".join(existing_config.public_sources) if existing_config else "remotive, arbeitnow",
            )
        )

        country_indeed = Prompt.ask(
            "[bold green]?[/bold green] Country for the Indeed and Glassdoor backends",
            default=existing_config.country_indeed if existing_config else "usa",
        )

        max_results = IntPrompt.ask(
            "[bold green]?[/bold green] Maximum listings per board per search term",
            default=existing_config.max_results_per_board if existing_config else 25,
        )

        salary_answer = Prompt.ask(
            "[bold green]?[/bold green] Minimum annual salary (optional; press Enter to skip)",
            default=str(existing_config.min_salary) if existing_config and existing_config.min_salary else "",
        ).strip()
        min_salary = int(salary_answer) if salary_answer.isdigit() else None

        proxy_answer = Prompt.ask(
            "[bold green]?[/bold green] Residential proxy URL (optional; press Enter to skip)",
            default=(existing_config.proxy_url if existing_config and existing_config.proxy_url else ""),
        ).strip()

        try:
            return SearchParameters(
                target_domains=domains,
                desired_experience_years=desired_experience,
                locations=locations,
                work_modes=work_modes,
                is_remote=work_modes == ["remote"],
                onsite_countries=onsite_countries,
                hours_old=hours_old,
                job_boards=job_boards,
                public_sources=public_sources,
                country_indeed=country_indeed,
                max_results_per_board=max_results,
                min_salary=min_salary,
                # Direct ATS boards are edited in searches.yaml rather than prompted
                # for; a company registry is tedious to enter one line at a time.
                ats_companies=existing_config.ats_companies if existing_config else {},
                proxy_url=proxy_answer or None,
            )
        except ValidationError as exc:
            console.print("\n[bold red]Those settings are not valid:[/bold red]")
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "input"
                console.print(f"  - [yellow]{field}[/yellow]: {error['msg']}")
            console.print("[dim]Let's try again.[/dim]\n")


def save_search_parameters(params: SearchParameters, output_path: Path) -> None:
    """Write validated search parameters to YAML."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        yaml.dump(params.model_dump(), handle, sort_keys=False, default_flow_style=False, allow_unicode=True)
    console.print(f"[bold green]Saved[/bold green] sourcing parameters to [yellow]{output_path}[/yellow]")


def load_search_parameters(config_path: Path) -> SearchParameters:
    """Load and validate search parameters from YAML.

    Validation errors are re-raised with the offending field named, since the most
    common cause is a hand-edited `searches.yaml`.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}. Run: python main.py configure")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping, got {type(raw).__name__}.")

    try:
        return SearchParameters(**raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"Invalid settings in {config_path.name} - {details}") from exc


def configure_cli(output_path: Optional[Path] = None) -> SearchParameters:
    """Entry point for interactive search parameterization."""
    target_path = output_path or settings.searches_path

    existing = None
    if target_path.exists():
        try:
            existing = load_search_parameters(target_path)
            console.print(f"[dim]Loaded previous parameters from {target_path}[/dim]")
        except Exception as exc:
            console.print(f"[yellow]Ignoring the existing {target_path.name}: {exc}[/yellow]")

    params = prompt_user_parameters(existing)
    save_search_parameters(params, target_path)
    return params
