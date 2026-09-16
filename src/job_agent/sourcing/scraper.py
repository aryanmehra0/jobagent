"""Omnichannel job sourcing engine.

Coordinates scraping across LinkedIn, Indeed, Glassdoor, and ZipRecruiter via
`python-jobspy`, folds in direct ATS endpoint feeds, enforces residential proxy
rotation, and filters out previously seen postings via the delta store.

Every constraint in `searches.yaml` is applied here, and the summary reports how
many postings each filter removed, so a sweep that returns nothing tells you which
constraint was responsible instead of just returning an empty list.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from job_agent.config.normalize import clean_text, strip_html
from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.config.settings import settings
from job_agent.sourcing.ats_direct import ATSDirectIngestion
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.sourcing.proxy_manager import ProxyManager

console = Console()

# JobSpy caps out well before this; requesting more just slows the sweep down.
MAX_RESULTS_PER_REQUEST = 100


class OmnichannelScraper:
    """Omnichannel job sourcing coordinator."""

    def __init__(
        self,
        search_params: Optional[SearchParameters] = None,
        proxy_manager: Optional[ProxyManager] = None,
        delta_store: Optional[DeltaStore] = None,
        ats_feeder: Optional[ATSDirectIngestion] = None,
    ):
        self.params = search_params or SearchParameters()
        self.proxy_mgr = proxy_manager or ProxyManager(proxy_source=self.params.proxy_url)
        self.delta_store = delta_store or DeltaStore()
        self.ats_feeder = ats_feeder or ATSDirectIngestion()
        # Counts why postings were discarded, reported at the end of the sweep.
        self.filter_stats: Counter = Counter()

    # --- Normalization --------------------------------------------------------

    def _normalize_jobspy_df(self, df: Any, default_source: str = "jobspy") -> List[JobPosting]:
        """Convert a JobSpy DataFrame into validated `JobPosting` models.

        Rows that fail validation (no URL, blank title) are counted and skipped
        rather than aborting the sweep, since one malformed row on one board should
        not cost the results from every other board.
        """
        import pandas as pd

        postings: List[JobPosting] = []
        if df is None or getattr(df, "empty", True):
            return postings

        for _, row in df.iterrows():
            def value(key: str) -> Any:
                if key not in row:
                    return None
                cell = row[key]
                return None if pd.isna(cell) else cell

            title = clean_text(value("title"))
            company = clean_text(value("company"))
            job_url = clean_text(value("job_url")) or clean_text(value("job_url_direct"))

            if not title or not company or not job_url:
                self.filter_stats["incomplete_row"] += 1
                continue

            description = value("description")
            # JobSpy returns markdown or HTML depending on the board.
            description_text = strip_html(description) if description else ""

            try:
                postings.append(
                    JobPosting(
                        id=JobPosting.create_id(job_url, company, title),
                        title=title,
                        company=company,
                        location=clean_text(value("location")) or "Remote",
                        job_url=job_url,
                        description=description_text,
                        date_posted=clean_text(value("date_posted")) or None,
                        is_remote=bool(value("is_remote")) if value("is_remote") is not None else False,
                        salary_min=_as_float(value("min_amount")),
                        salary_max=_as_float(value("max_amount")),
                        salary_currency=clean_text(value("currency")) or "USD",
                        job_type=clean_text(value("job_type")) or None,
                        source=clean_text(value("site")) or default_source,
                    )
                )
            except Exception as exc:  # pydantic ValidationError and friends
                self.filter_stats["invalid_row"] += 1
                console.print(f"[dim]Skipped malformed listing from {default_source}: {exc}[/dim]")

        return postings

    # --- Board scraping -------------------------------------------------------

    def scrape_job_boards(self) -> List[JobPosting]:
        """Scrape every configured board for every domain and location.

        Boards are queried one at a time so each keeps its own sticky proxy session
        and a block on one board cannot cost the results of the others.
        """
        import jobspy

        results: List[JobPosting] = []
        boards = self.params.job_boards
        locations = self.params.locations or ["Remote"]
        per_request = min(self.params.max_results_per_board, MAX_RESULTS_PER_REQUEST)

        for domain in self.params.target_domains:
            for location in locations:
                console.print(
                    f"[bold cyan]Sourcing:[/bold cyan] '{domain}' in '{location}' "
                    f"across [yellow]{', '.join(boards)}[/yellow]"
                )
                for board in boards:
                    results.extend(
                        self._scrape_single_board(
                            jobspy, board=board, domain=domain, location=location, results_wanted=per_request
                        )
                    )

        return results

    def _scrape_single_board(
        self,
        jobspy: Any,
        *,
        board: str,
        domain: str,
        location: str,
        results_wanted: int,
    ) -> List[JobPosting]:
        """Query one board once, rotating the proxy and retrying on a rate limit."""
        for attempt in (1, 2):
            proxy = self.proxy_mgr.get_proxy_for_board(board)
            try:
                console.print(f"  - {board} (proxy: {proxy or 'direct'})...")
                df = jobspy.scrape_jobs(
                    site_name=[board],
                    search_term=domain,
                    location=location,
                    is_remote=self.params.is_remote,
                    results_wanted=results_wanted,
                    hours_old=self.params.hours_old,
                    country_indeed=self.params.country_indeed,
                    linkedin_fetch_description=(board == "linkedin"),
                    proxies=[proxy] if proxy else None,
                    description_format="markdown",
                    verbose=0,
                )
                postings = self._normalize_jobspy_df(df, default_source=board)
                console.print(f"    [green]Found {len(postings)} listings on {board}[/green]")
                return postings
            except Exception as exc:
                message = str(exc)
                rate_limited = any(token in message for token in ("429", "403", "blocked", "Too Many Requests"))
                if rate_limited and attempt == 1 and self.proxy_mgr.has_proxies:
                    console.print(f"    [yellow]{board} rate-limited; rotating proxy and retrying...[/yellow]")
                    self.proxy_mgr.mark_proxy_failed(board, proxy)
                    continue
                console.print(f"    [yellow]Warning on {board}: {message[:160]}[/yellow]")
                if rate_limited:
                    self.proxy_mgr.mark_proxy_failed(board, proxy)
                return []
        return []

    # --- Filtering ------------------------------------------------------------

    def _passes_filters(self, job: JobPosting) -> bool:
        """Apply the `searches.yaml` constraints the boards do not enforce themselves.

        Unknown values never disqualify a posting: a listing with no salary band is
        kept, because most listings omit one and dropping them would discard the
        majority of real opportunities.
        """
        if self.params.is_remote and not job.is_remote:
            self.filter_stats["not_remote"] += 1
            return False

        if self.params.min_salary is not None:
            # Compare against the top of the band: a range of 120k-200k satisfies a
            # 175k floor, even though its lower bound does not.
            ceiling = job.salary_max if job.salary_max is not None else job.salary_min
            if ceiling is not None and ceiling < self.params.min_salary:
                self.filter_stats["below_min_salary"] += 1
                return False

        age_hours = job.age_hours()
        if age_hours is not None and age_hours > self.params.hours_old:
            self.filter_stats["too_old"] += 1
            return False

        return True

    @staticmethod
    def _deduplicate(jobs: List[JobPosting]) -> Tuple[List[JobPosting], int]:
        """Collapse postings that share an ID within a single sweep.

        The same role legitimately appears on several boards and under several
        search terms; without this, one posting would be evaluated several times.
        """
        seen: Dict[str, JobPosting] = {}
        duplicates = 0
        for job in jobs:
            existing = seen.get(job.id)
            if existing is None:
                seen[job.id] = job
                continue
            duplicates += 1
            # Prefer the copy that carries the richer description.
            if len(job.description) > len(existing.description):
                seen[job.id] = job
        return list(seen.values()), duplicates

    # --- Orchestration --------------------------------------------------------

    def run_sourcing_pipeline(
        self,
        include_ats_direct: bool = True,
        output_file: Optional[Path] = None,
    ) -> List[JobPosting]:
        """Execute a full omnichannel sourcing sweep.

        Boards and ATS feeds are queried, results are filtered and de-duplicated
        within the sweep and against the delta store, and the surviving novel
        postings are written to disk for Phase 3.
        """
        target_output = output_file or (settings.outputs_dir / "scraped_jobs.json")
        self.filter_stats.clear()

        console.print("[bold cyan]=== Omnichannel sourcing sweep started ===[/bold cyan]")
        console.print(f"Target domains : {', '.join(self.params.target_domains)}")
        console.print(f"Locations      : {', '.join(self.params.locations)}")
        console.print(f"Temporal window: {self.params.hours_old} hours")
        if self.params.min_salary:
            console.print(f"Minimum salary : {self.params.min_salary:,}")

        aggregated: List[JobPosting] = []

        try:
            aggregated.extend(self.scrape_job_boards())
        except ImportError:
            console.print(
                "[red]python-jobspy is not installed.[/red] Install it with: pip install python-jobspy"
            )
        except Exception as exc:
            console.print(f"[red]JobSpy scraping error: {exc}[/red]")

        if include_ats_direct:
            console.print("[cyan]Querying direct ATS endpoints (Greenhouse, Lever, Ashby)...[/cyan]")
            try:
                ats_jobs = self.ats_feeder.scrape_configured_ats(
                    companies=self.params.ats_companies or None,
                    search_params=self.params,
                )
                console.print(f"[green]Direct ATS feeds provided {len(ats_jobs)} matching listings[/green]")
                aggregated.extend(ats_jobs)
            except Exception as exc:
                console.print(f"[yellow]Direct ATS query issue: {exc}[/yellow]")

        raw_count = len(aggregated)

        filtered = [job for job in aggregated if self._passes_filters(job)]
        deduped, in_sweep_duplicates = self._deduplicate(filtered)
        unseen = self.delta_store.filter_unseen(deduped)
        previously_seen = len(deduped) - len(unseen)

        self._print_summary(raw_count, in_sweep_duplicates, previously_seen, unseen)

        if unseen:
            self.delta_store.mark_many_seen(unseen, status="scraped")

        target_output.parent.mkdir(parents=True, exist_ok=True)
        target_output.write_text(
            json.dumps([job.model_dump() for job in unseen], indent=2), encoding="utf-8"
        )
        console.print(f"[bold green]Sourced jobs written to:[/bold green] [yellow]{target_output}[/yellow]\n")

        return unseen

    def _print_summary(
        self,
        raw_count: int,
        in_sweep_duplicates: int,
        previously_seen: int,
        unseen: List[JobPosting],
    ) -> None:
        """Render a funnel showing where every discovered posting went."""
        table = Table(title="Sourcing funnel", show_header=True, header_style="bold magenta")
        table.add_column("Stage", style="cyan")
        table.add_column("Count", justify="right", style="white")

        table.add_row("Raw listings discovered", str(raw_count))
        labels = {
            "not_remote": "Dropped: not remote",
            "below_min_salary": "Dropped: below minimum salary",
            "too_old": "Dropped: older than freshness window",
            "incomplete_row": "Dropped: missing title/company/URL",
            "invalid_row": "Dropped: failed schema validation",
        }
        for key, label in labels.items():
            if self.filter_stats.get(key):
                table.add_row(label, f"-{self.filter_stats[key]}")
        if in_sweep_duplicates:
            table.add_row("Dropped: duplicates within this sweep", f"-{in_sweep_duplicates}")
        if previously_seen:
            table.add_row("Dropped: already in delta store", f"-{previously_seen}")
        table.add_row("[bold green]Novel jobs ready for evaluation[/bold green]", f"[bold green]{len(unseen)}[/bold green]")

        console.print(table)

        missing_descriptions = sum(1 for job in unseen if len(job.description) < 50)
        if missing_descriptions:
            console.print(
                f"[yellow]Note:[/yellow] {missing_descriptions} listing(s) have little or no description text; "
                "their semantic match scores will be less reliable."
            )


def _as_float(value: Any) -> Optional[float]:
    """Coerce a DataFrame cell to a non-negative float, or None if it is not numeric."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
